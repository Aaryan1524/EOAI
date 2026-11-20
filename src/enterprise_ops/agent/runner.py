"""Run one model against one bounded sandbox session."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from enterprise_ops.agent.handles import ToolHandles
from enterprise_ops.agent.harness import Harness, load_harness
from enterprise_ops.agent.model import Model, ModelError, OllamaModel, strict_json
from enterprise_ops.agent.output import Ledger, RunOutput
from enterprise_ops.agent.trace import RunTrace
from enterprise_ops.domain.entities import Capability, Principal
from enterprise_ops.sandbox import FaultPlan, RunSandbox, Snapshot
from enterprise_ops.storage.audit import canonical
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
        try:
            self.request.encode("utf-8")
            if self.customer_query is not None:
                self.customer_query.encode("utf-8")
        except (UnicodeError, AttributeError) as error:
            raise ValueError("Task text must be valid UTF-8") from error
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
    tool_retries: int = 2
    model_corrections: int = 3

    def __post_init__(self) -> None:
        if any(type(limit) is not int or limit < 1 for limit in (
            self.tool_calls, self.model_turns, self.total_tokens, self.output_tokens_per_turn,
        )) or not 0 < self.deadline_seconds <= 3600:
            raise ValueError("Run limits must be positive and the deadline must be at most one hour")
        if any(type(limit) is not int or not 0 <= limit <= maximum for limit, maximum in (
            (self.tool_retries, 2), (self.model_corrections, 3),
        )):
            raise ValueError("Recovery allows up to two tool retries and three model corrections")


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
        limits: RunLimits, trace: RunTrace, run_id: str, business_date: str,
    ) -> None:
        self.gateway = gateway
        self.model = model
        self.task = task
        self.harness = harness
        self.limits = limits
        self.trace = trace
        self.run_id = run_id
        self.business_date = business_date
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
        self.corrections = 0
        self.invalid_calls: set[str] = set()
        self.write_stop: str | None = None
        self.handles = ToolHandles()

    @property
    def remaining(self) -> float:
        return self.deadline - monotonic()

    def error(self, code: str, **fields) -> None:
        value = {"code": code, **fields}
        self.errors.append(value)
        self.trace.event("error", **value)

    def dispatch(self, name: str, args: dict | None) -> ToolResponse:
        self.tool_calls += 1
        response = self.gateway.invoke(name, args, timeout=max(0.0, self.remaining))
        fields = self._fields.get(name, ())
        trace_name = name if name in self._fields else "unknown"
        self.trace.event(
            "tool", tool=trace_name,
            arguments={key: value for key, value in (args or {}).items() if key in fields},
            response=response.model_dump(mode="json"),
        )
        recorded_args = self.gateway.canonical_arguments(name, args) if response.ok else args
        self.ledger.record(name, recorded_args, response)
        if not response.ok:
            self.error(response.error.code, tool=trace_name, call_id=response.meta.call_id)
        return response

    def call_tool(self, name: str, args: dict | None) -> tuple[ToolResponse, bool]:
        uncertain = False
        for attempt in range(self.limits.tool_retries + 1):
            response = self.dispatch(name, args)
            error = response.error
            if response.ok or not error.retryable or error.code not in {"RATE_LIMITED", "TRANSIENT_FAILURE"}:
                return response, uncertain
            uncertain |= error.code == "TRANSIENT_FAILURE"
            delay = (error.retry_after_ms or 0) / 1000
            reserve = 2 if name in _WRITES else 1
            if (
                attempt == self.limits.tool_retries or self.remaining <= delay
                or self.tool_calls + reserve > self.limits.tool_calls
            ):
                return response, uncertain
            self.trace.event("retry", tool=name, after_call_id=response.meta.call_id, delay_ms=delay * 1000)
            if delay:
                sleep(delay)
        raise AssertionError("Bounded tool retry must produce a response")

    def act(self, name: str, args: dict | None) -> ToolResponse:
        args = self.handles.for_gateway(args)
        response, uncertain = self.call_tool(name, args)
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
                readback, _ = self.call_tool(tool, {argument: response.data.result_id})
                if not readback.ok:
                    self.write_stop = "verification_incomplete"
            else:
                self.write_stop = "verification_incomplete"
        else:
            if args and isinstance(args.get("preview_token"), str):
                invoice = self.previews.get(args["preview_token"])
                if invoice is not None:
                    self.ledger.record_failure(invoice, name, uncertain=uncertain, reason=response.error.code)
            if uncertain or response.error.retryable:
                self.write_stop = "commit_unknown" if uncertain else "write_retry_limit"
        return response

    def feedback(self, transcript: list[dict], code: str, message: str, **fields: str) -> bool:
        if self.corrections == self.limits.model_corrections or self.model_turns >= self.limits.model_turns:
            return False
        self.corrections += 1
        transcript.append({"role": "user", "content": canonical({"runtime_feedback": message, **fields})})
        self.trace.event("model_correction", code=code, **fields)
        return True

    def output(self, choice: FinalChoice | None, stop: str) -> RunOutput:
        return self.ledger.finish(
            run_id=self.run_id, task_id=self.task.id, as_of=self.trace.as_of,
            business_date=self.business_date, lookup=self.gateway.observation,
            selected_customer_id=choice.customer_id if choice else None,
            review_reason=choice.review_reason if choice else None, stop_reason=stop,
        )

    def completion_feedback(self, transcript: list[dict], choice: FinalChoice) -> bool:
        result = self.output(choice, "model_final")
        missing = {item.code for item in result.missing_information}
        if "CUSTOMER_SEARCH_INCOMPLETE" in missing:
            message = (
                "Finish the original customer search. Copy the latest continuation "
                "and keep the same query and page size."
            )
        elif "INVOICE_SEARCH_INCOMPLETE" in missing:
            message = (
                "Finish the invoice search. For the full account, omit status filters "
                "and follow each continuation with the same search fields and page size."
            )
        elif "ASSESSMENT_MISSING" in missing:
            message = "Assess the remaining in-scope invoices before finishing."
        elif (
            self.task.write_scope
            and Capability.CREATE_TICKET in self.task.write_scope.capabilities
            and "ACTION_NOT_ATTEMPTED" in missing
            and self.tool_calls + 2 <= self.limits.tool_calls
        ):
            remaining = [
                action.invoice_id for action in result.actions
                if action.type == "create_escalation_ticket" and action.status == "not_attempted"
            ]
            return self.feedback(
                transcript, "ESCALATION_INCOMPLETE",
                "Eligible invoices still need an action. Check for an active ticket or preview "
                "creation using the current assessment evidence. Submit a ready preview if appropriate.",
                invoice_ids=", ".join(remaining),
            )
        else:
            return False
        return self.feedback(transcript, "COVERAGE_INCOMPLETE", message)

    def execute(self) -> tuple[FinalChoice | None, str]:
        transcript = [{"role": "user", "content": canonical({
            "request": self.task.request, **self.task.public_context(),
        })}]
        seen_calls: set[str] = set()
        offered_followup = False
        checked_completion = False
        offered_token: str | None = None
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
            if any(type(count) is not int or count < 0 for count in (turn.input_tokens, turn.output_tokens)):
                self.error("INVALID_MODEL_USAGE")
                return None, "invalid_turn"
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
                submission = _submission_choice(turn.final) if offered_token is not None else None
                if submission is not None:
                    token, offered_token = offered_token, None
                    if not submission:
                        return FinalChoice(customer_id=self.task.account, review_reason=None), "model_final"
                    if self.tool_calls + 2 > self.limits.tool_calls:
                        return None, "tool_budget"
                    tool, recommended = self.offers[token]
                    self.trace.event("model_action_choice", tool=tool, invoice_id=self.previews[token])
                    response = self.act(tool, recommended)
                    if self.write_stop:
                        return None, self.write_stop
                    transcript.append({"role": "user", "content": canonical({
                        "runtime_feedback": "The chosen tool returned this result. Finish the original task.",
                        "result": self.handles.for_model(response.model_dump(mode="json")),
                    })})
                    continue
                try:
                    choice = _final_choice(turn.final)
                except (ValidationError, ValueError, RecursionError):
                    self.error("INVALID_FINAL_CHOICE")
                    if self.feedback(
                        transcript, "INVALID_FINAL_CHOICE",
                        'Return only the requested JSON object with customer_id and review_reason. A missing value is JSON null.',
                    ):
                        continue
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
                                "The authorized action is still open. Choose exactly one JSON object "
                                'with a boolean. {"submit_preview":true} submits this already-authorized preview. '
                                '{"submit_preview":false} leaves it open. You can also call the declared tool explicitly.'
                            ),
                            "pending_tool": tool,
                            "provider_tool_name": tool.replace(".", "_"),
                            "arguments": self.handles.for_model(recommended),
                        })})
                        self.trace.event("action_followup", tool=tool, invoice_id=self.previews[pending])
                        offered_followup = True
                        offered_token = pending
                        continue
                if (
                    self.harness.version == "v3" and choice.review_reason is None
                    and not checked_completion and self.tool_calls < self.limits.tool_calls
                    and self.completion_feedback(transcript, choice)
                ):
                    checked_completion = True
                    continue
                self.trace.event("model_final", choice=choice.model_dump())
                return choice, "model_final"
            if len(turn.calls) != 1 or turn.final is not None or turn.calls[0].id in seen_calls:
                self.error("INVALID_MODEL_TURN")
                if self.feedback(transcript, "INVALID_MODEL_TURN", "Propose a single tool call or one final JSON object."):
                    continue
                return None, "invalid_turn"
            call = turn.calls[0]
            offered_token = None
            seen_calls.add(call.id)
            reserve = 2 if call.name in _WRITES else 1
            if self.tool_calls + reserve > self.limits.tool_calls:
                return None, "tool_budget"
            args = _arguments(call.arguments)
            response = self.act(call.name, args)
            if self.write_stop:
                return None, self.write_stop
            transcript.extend((
                {
                    "type": "function_call", "call_id": call.id, "name": call.name,
                    "arguments": canonical(args if args is not None else {}),
                },
                {
                    "type": "function_call_output", "call_id": call.id, "name": call.name,
                    "output": canonical(self.handles.for_model(response.model_dump(mode="json"))),
                },
            ))
            if not response.ok and response.error.code in {"VALIDATION_ERROR", "SCHEMA_MISMATCH"}:
                invalid = digest(json.dumps([call.name, args], sort_keys=True))
                message = "Use a declared tool name and its exact argument schema. Correct the rejected call once."
                if self.harness.version == "v3" and response.error.message == "Invalid pagination cursor":
                    message = (
                        "Copy the latest continuation from this same search. Repeat its original "
                        "arguments exactly, including page size and any filters. If needed, restart without a cursor."
                    )
                if invalid in self.invalid_calls or not self.feedback(
                    transcript, response.error.code,
                    message,
                    tool=call.name if call.name in self._fields else "unknown",
                ):
                    return None, "invalid_tool"
                self.invalid_calls.add(invalid)
        return None, "model_budget"


def _arguments(raw: str) -> dict | None:
    if len(raw) > 8192:
        return None
    try:
        value = strict_json(raw)
        if not isinstance(value, dict):
            return None
        return value if _bounded_json(value) else None
    except (ValueError, RecursionError):
        return None


def _final_choice(raw: str) -> FinalChoice:
    if len(raw) > 4096:
        raise ValueError("Final choice exceeds limit")
    choice = _json_choice(raw)
    if isinstance(choice, dict) and choice.get("review_reason") == "null":
        choice["review_reason"] = None
    return FinalChoice.model_validate(choice)


def _json_choice(raw: str):
    final = raw.strip()
    if final.startswith("```json\n") and final.endswith("\n```"):
        final = final[8:-4].strip()
    return strict_json(final)


def _submission_choice(raw: str) -> bool | None:
    if len(raw) <= 4096:
        try:
            value = _json_choice(raw)
            if isinstance(value, dict) and set(value) == {"submit_preview"} and type(value["submit_preview"]) is bool:
                return value["submit_preview"]
        except (ValueError, RecursionError):
            pass
    return None


def _bounded_json(value: object, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, dict):
        return all(_bounded_json(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return all(_bounded_json(item, depth + 1) for item in value)
    return not isinstance(value, float) or abs(value) < float("inf")


def task_digest(task: RunTask) -> str:
    return digest({
        "task_id": task.id, "request": task.request, "context": task.public_context(),
        "principal": asdict(task.principal),
        "write_scope_digest": task.write_scope.digest if task.write_scope else None,
    })


def fault_plan_digest(faults: FaultPlan | None) -> str:
    return digest([asdict(step) for step in faults.steps] if faults is not None else [])


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
    model_digest: str | None = None,
    scenario: str | None = None,
) -> RunResult:
    limits = limits or RunLimits(tool_calls=12 if task.operation == "list_invoices" else 35)
    harness = harness or load_harness()
    if sha256(harness.instructions.encode()).hexdigest() != harness.digest:
        raise ValueError("Harness instructions do not match their digest")
    if isinstance(model, OllamaModel):
        model_digest = model_digest or model.model_digest(timeout=min(5.0, limits.deadline_seconds))
        model_digest = model.pin_model_digest(model_digest)
    sandbox = snapshot.clone(runs_root / "sandboxes", run_id)
    with closing(connect(sandbox.path)) as db:
        authorize(db, task.principal, Capability.READ)
        timezone = db.execute("SELECT timezone FROM tenants WHERE id = ?", (task.principal.tenant_id,)).fetchone()[0]
    gateway = ToolGateway(sandbox, task.principal, write_scope=task.write_scope, faults=faults)
    trace = RunTrace(runs_root / "traces", sandbox)
    run = _Run(
        gateway, model, task, harness, limits, trace, run_id,
        sandbox.clock.business_date(timezone).isoformat(),
    )
    trace.write("manifest", {
        "manifest_version": 1, "run_id": run_id, "task_id": task.id, "sandbox_id": sandbox.id,
        "scenario": scenario, "task_digest": task_digest(task),
        "fault_plan_digest": fault_plan_digest(faults),
        "write_scope_digest": task.write_scope.digest if task.write_scope else None,
        "snapshot_id": snapshot.id, "snapshot_digest": snapshot.digest, "profile": sandbox.profile,
        "tool_declarations_digest": digest(run.declarations), "as_of": trace.as_of,
        "provider": model.provider, "model_id": model.model_id, "model_digest": model_digest,
        "model_settings": getattr(model, "settings", None),
        "harness": {"version": harness.version, "digest": harness.digest},
        "limits": asdict(limits),
    })
    trace.write("input", {
        "task": task.public_context(), "request": task.request, "tenant_id": task.principal.tenant_id,
        "user_id": task.principal.user_id, "rendered_instructions": harness.instructions,
    })
    choice, stop = run.execute()
    output = run.output(choice, stop)
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
