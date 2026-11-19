"""Run one model against one bounded sandbox session."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from enterprise_ops.agent.harness import Harness, load_harness
from enterprise_ops.agent.model import Model, ModelError
from enterprise_ops.agent.output import Ledger, RunOutput
from enterprise_ops.agent.trace import RunTrace
from enterprise_ops.domain.entities import Capability, Principal
from enterprise_ops.sandbox import FaultPlan, RunSandbox, Snapshot
from enterprise_ops.storage.audit import canonical, stamp
from enterprise_ops.storage.repository import authorize
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools.contracts import Id, StrictModel, ToolResponse
from enterprise_ops.tools.gateway import ToolGateway
from enterprise_ops.tools.session import WriteScope, digest


@dataclass(frozen=True)
class RunTask:
    id: str
    request: str
    principal: Principal
    operation: Literal["list_invoices", "review_invoices", "escalate"]
    customer_query: str | None = None
    customer_id: str | None = None
    invoice_ids: frozenset[str] | None = None
    write_scope: WriteScope | None = None

    def __post_init__(self) -> None:
        identifier = TypeAdapter(Id)
        try:
            identifier.validate_python(self.id, strict=True)
        except ValidationError as error:
            raise ValueError("Task ID must be a valid bounded identifier") from error
        if not isinstance(self.request, str) or not self.request.strip() or len(self.request) > 4096:
            raise ValueError("Task requires an ID and a bounded request")
        if self.operation not in ("list_invoices", "review_invoices", "escalate"):
            raise ValueError("Unsupported task operation")
        if self.invoice_ids is not None:
            object.__setattr__(self, "invoice_ids", frozenset(self.invoice_ids))
        if self.write_scope and (
            self.operation != "escalate" or self.write_scope.principal != self.principal
            or self.customer_id not in (None, self.write_scope.customer_id)
            or (self.invoice_ids is not None and self.invoice_ids != self.write_scope.invoice_ids)
        ):
            raise ValueError("Task and write scope do not agree")
        try:
            if self.account is not None:
                identifier.validate_python(self.account, strict=True)
            for invoice_id in self.assigned_invoices or ():
                identifier.validate_python(invoice_id, strict=True)
        except ValidationError as error:
            raise ValueError("Task customer and invoice IDs must be valid bounded identifiers") from error

    @property
    def account(self) -> str | None:
        return self.write_scope.customer_id if self.write_scope else self.customer_id

    @property
    def assigned_invoices(self) -> frozenset[str] | None:
        return self.write_scope.invoice_ids if self.write_scope else self.invoice_ids

    def public_context(self) -> dict:
        return {
            "operation": self.operation,
            "customer_query": self.customer_query,
            "customer_id": self.account,
            "invoice_ids": sorted(self.assigned_invoices) if self.assigned_invoices is not None else None,
            "authorized_actions": sorted(self.write_scope.capabilities) if self.write_scope else [],
        }


@dataclass(frozen=True)
class RunLimits:
    tool_calls: int = 35
    model_turns: int = 40
    total_tokens: int = 300_000
    output_tokens_per_turn: int = 768
    deadline_seconds: float = 600.0

    def __post_init__(self) -> None:
        if any(type(limit) is not int or limit < 1 for limit in (
            self.tool_calls, self.model_turns, self.total_tokens, self.output_tokens_per_turn,
        )) or not 0 < self.deadline_seconds <= 3600:
            raise ValueError("Run limits must be positive and the deadline must be at most one hour")


class FinalChoice(StrictModel):
    customer_id: Id | None
    review_reason: Literal["AMBIGUOUS_CUSTOMER", "MISSING_INFORMATION"] | None


@dataclass(frozen=True)
class RunResult:
    output: RunOutput
    trace_path: Path
    sandbox: RunSandbox


_WRITES = {
    "ticket.create": ("ticket.get", "ticket_id"),
    "approval.request": ("approval.get", "approval_id"),
}


class _Run:
    def __init__(
        self, gateway: ToolGateway, model: Model, task: RunTask, harness: Harness,
        limits: RunLimits, trace: RunTrace,
    ) -> None:
        self.gateway = gateway
        self.model = model
        self.task = task
        self.harness = harness
        self.limits = limits
        self.trace = trace
        self.declarations = gateway.declarations()
        self._fields = {
            tool["name"]: frozenset(tool["arguments"].get("properties", {})) for tool in self.declarations
        }
        self.ledger = Ledger(task.operation, task.customer_query, task.account, task.assigned_invoices)
        self.tool_calls = 0
        self.model_turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.errors: list[dict] = []
        self.deadline = monotonic() + limits.deadline_seconds
        self.previews: dict[str, str] = {}
        self.offers: dict[str, tuple[str, dict]] = {}
        self.attempted: set[str] = set()

    @property
    def remaining(self) -> float:
        return self.deadline - monotonic()

    def error(self, code: str, **fields) -> None:
        value = {"code": code, **fields}
        self.errors.append(value)
        self.trace.event("error", **value)

    def dispatch(self, name: str, args: dict | None) -> ToolResponse:
        self.tool_calls += 1
        response = self.gateway.invoke(name, args)
        fields = self._fields.get(name, ())
        trace_name = name if name in self._fields else "unknown"
        self.trace.event(
            "tool", tool=trace_name,
            arguments={key: value for key, value in (args or {}).items() if key in fields},
            response=response.model_dump(mode="json"),
        )
        self.ledger.record(name, args or {}, response)
        if not response.ok:
            self.error(response.error.code, tool=trace_name, call_id=response.meta.call_id)
        return response

    def act(self, name: str, args: dict | None) -> ToolResponse:
        response = self.dispatch(name, args)
        if name == "ticket.preview_create" and response.ok and response.data.preview_token is not None:
            preview = response.data
            token = preview.preview_token
            self.previews[token] = preview.invoice_id
            command = "ticket.create" if preview.decision == "ready_to_create" else "approval.request"
            self.offers[token] = (command, {
                "preview_token": token, "idempotency_key": preview.idempotency_key,
                "expected_tenant_revision": preview.tenant_revision,
                "expected_invoice_version": preview.invoice_version,
                "expected_policy_version": preview.policy_version,
            })
        if name not in _WRITES:
            return response
        if args and isinstance(args.get("preview_token"), str):
            self.attempted.add(args["preview_token"])
        if response.ok:
            if self.remaining > 0 and self.tool_calls < self.limits.tool_calls:
                tool, argument = _WRITES[name]
                self.dispatch(tool, {argument: response.data.result_id})
        elif args and isinstance(args.get("preview_token"), str):
            invoice = self.previews.get(args["preview_token"])
            if invoice is not None:
                self.ledger.record_failure(invoice, name, uncertain=response.error.retryable, reason=response.error.code)
        return response

    def execute(self) -> tuple[FinalChoice | None, str]:
        transcript = [{"role": "user", "content": canonical({
            "request": self.task.request, **self.task.public_context(),
        })}]
        seen_calls: set[str] = set()
        offered_followup = False
        for _ in range(self.limits.model_turns):
            if self.remaining <= 0:
                return None, "deadline"
            tokens_left = self.limits.total_tokens - self.input_tokens - self.output_tokens
            if tokens_left <= 0:
                return None, "token_budget"
            self.model_turns += 1
            try:
                turn = self.model.turn(
                    instructions=self.harness.instructions, transcript=tuple(transcript),
                    declarations=self.declarations, timeout=self.remaining,
                    max_output_tokens=min(tokens_left, self.limits.output_tokens_per_turn),
                )
            except ModelError:
                self.error("MODEL_ERROR")
                return None, "provider_error"
            self.input_tokens += turn.input_tokens
            self.output_tokens += turn.output_tokens
            self.trace.event(
                "model_turn", input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
                proposed_tools=[call.name if call.name in self._fields else "unknown" for call in turn.calls],
            )
            if self.remaining <= 0:
                return None, "deadline"
            if self.input_tokens + self.output_tokens > self.limits.total_tokens:
                return None, "token_budget"
            if not turn.calls and turn.final is not None:
                try:
                    choice = _final_choice(turn.final)
                except (ValidationError, ValueError, RecursionError):
                    self.error("INVALID_FINAL_CHOICE")
                    return None, "invalid_final"
                if (
                    choice.review_reason is None and not offered_followup
                    and self.tool_calls + 2 <= self.limits.tool_calls and self.model_turns < self.limits.model_turns
                ):
                    pending = next((key for key in reversed(self.offers) if key not in self.attempted), None)
                    if pending is not None:
                        tool, recommended = self.offers[pending]
                        transcript.append({"role": "user", "content": canonical({
                            "runtime_feedback": (
                                "The authorized action is still open. If you intend to submit it, "
                                "call the indicated declared tool with these exact arguments now. "
                                "Otherwise return only the final JSON with unquoted null when appropriate."
                            ),
                            "pending_tool": tool,
                            "provider_tool_name": tool.replace(".", "_"),
                            "arguments": recommended,
                        })})
                        self.trace.event("action_followup", tool=tool, invoice_id=self.previews[pending])
                        offered_followup = True
                        continue
                self.trace.event("model_final", choice=choice.model_dump())
                return choice, "model_final"
            if len(turn.calls) != 1 or turn.final is not None or turn.calls[0].id in seen_calls:
                self.error("INVALID_MODEL_TURN")
                return None, "invalid_turn"
            call = turn.calls[0]
            seen_calls.add(call.id)
            reserve = 2 if call.name in _WRITES else 1
            if self.tool_calls + reserve > self.limits.tool_calls:
                return None, "tool_budget"
            args = _arguments(call.arguments)
            response = self.act(call.name, args)
            transcript.extend((
                {
                    "type": "function_call", "call_id": call.id, "name": call.name,
                    "arguments": canonical(args if args is not None else {}),
                },
                {
                    "type": "function_call_output", "call_id": call.id, "name": call.name,
                    "output": response.model_dump_json(),
                },
            ))
        return None, "model_budget"


def _arguments(raw: str) -> dict | None:
    if len(raw) > 8192:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        return value if _bounded_json(value) else None
    except (ValueError, RecursionError):
        return None


def _final_choice(raw: str) -> FinalChoice:
    if len(raw) > 4096:
        raise ValueError("Final choice exceeds limit")
    final = raw.strip()
    if final.startswith("```json\n") and final.endswith("\n```"):
        final = final[8:-4].strip()
    choice = json.loads(final)
    if isinstance(choice, dict) and choice.get("review_reason") == "null":
        choice["review_reason"] = None
    return FinalChoice.model_validate(choice)


def _bounded_json(value: object, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, dict):
        return all(_bounded_json(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return all(_bounded_json(item, depth + 1) for item in value)
    return not isinstance(value, float) or abs(value) < float("inf")


def run_task(
    snapshot: Snapshot,
    task: RunTask,
    model: Model,
    *,
    run_id: str,
    runs_root: Path,
    limits: RunLimits | None = None,
    harness: Harness | None = None,
    faults: FaultPlan | None = None,
) -> RunResult:
    limits = limits or RunLimits(tool_calls=12 if task.operation == "list_invoices" else 35)
    harness = harness or load_harness()
    sandbox = snapshot.clone(runs_root / "sandboxes", run_id)
    with closing(connect(sandbox.path)) as db:
        authorize(db, task.principal, Capability.READ)
        timezone = db.execute("SELECT timezone FROM tenants WHERE id = ?", (task.principal.tenant_id,)).fetchone()[0]
    gateway = ToolGateway(sandbox, task.principal, write_scope=task.write_scope, faults=faults)
    trace = RunTrace(runs_root / "traces", sandbox)
    run = _Run(gateway, model, task, harness, limits, trace)
    trace.write("manifest", {
        "run_id": run_id, "task_id": task.id, "sandbox_id": sandbox.id,
        "snapshot_id": snapshot.id, "snapshot_digest": snapshot.digest, "profile": sandbox.profile,
        "tool_declarations_digest": digest(run.declarations), "as_of": trace.as_of,
        "provider": model.provider, "model_id": model.model_id,
        "harness": {"version": harness.version, "digest": harness.digest},
        "limits": asdict(limits),
    })
    trace.write("input", {
        "task": task.public_context(), "request": task.request, "tenant_id": task.principal.tenant_id,
        "user_id": task.principal.user_id, "rendered_instructions": harness.instructions,
    })
    choice, stop = run.execute()
    output = run.ledger.finish(
        run_id=run_id, task_id=task.id, as_of=stamp(sandbox.clock.instant),
        business_date=sandbox.clock.business_date(timezone).isoformat(), lookup=gateway.observation,
        selected_customer_id=choice.customer_id if choice else None,
        review_reason=choice.review_reason if choice else None, stop_reason=stop,
    )
    final = output.model_dump(mode="json")
    trace.write("final_output", final)
    trace.write("evidence", final["evidence"])
    trace.write("errors", run.errors)
    trace.write("summary", {
        "stop_reason": stop, "tool_calls": run.tool_calls, "model_turns": run.model_turns,
        "input_tokens": run.input_tokens, "output_tokens": run.output_tokens, "status": output.status,
    })
    trace.state_delta(sandbox, task.principal.tenant_id)
    return RunResult(output, trace.path, sandbox)
