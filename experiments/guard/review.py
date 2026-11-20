"""Reference-free GUARD task checklists and two-layer EOAI rollout review."""

from collections import Counter
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import file_digest
import json
from pathlib import Path
import re
import sqlite3

from enterprise_ops.agent.model import strict_json
from enterprise_ops.agent.output import ACTION_TYPES, RunOutput
from enterprise_ops.agent.runner import RunTask, task_digest
from enterprise_ops.agent.trace import scrub
from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import Capability
from enterprise_ops.storage.repository import Forbidden, authorize, load_assessment
from enterprise_ops.tools.contracts import ToolResponse
from enterprise_ops.tools.gateway import TOOLS, declarations_for_profile
from enterprise_ops.tools.profiles import TOOL_PROFILES
from pydantic import ValidationError

from .semantic import SemanticAttempt, grade_criterion

Complete = Callable[[str, str, str, dict, float], dict]

_CHECKLIST = """Select semantic checks from semantic_options. Use the whole request to interpret each
phrase. Select policy qualifiers, exceptions, evidence, and explanations. Omit requirements
deterministic_criteria already covers. Return only {"selected":["s1","s2"]} with the distinct option
IDs you want to check. An empty list is valid when deterministic checks cover the request.
"""

_CONTEXT_KEYS = {"operation", "customer_query", "customer_id", "invoice_ids", "authorized_actions"}
_INVALID_ARGUMENTS = {"NOT_FOUND", "VALIDATION_ERROR", "SCHEMA_MISMATCH", "PERMISSION_DENIED"}
_MUTABLE_TABLES = {"tenants", "audit_events", "escalation_tickets", "approval_requests", "command_receipts"}
_ACTION_TABLES = {"ticket.create": "escalation_tickets", "approval.request": "approval_requests"}
_READBACK = {"ticket.create": "ticket.get", "approval.request": "approval.get"}


@dataclass(frozen=True)
class Criterion:
    id: str
    description: str
    hook: str | None = None


@dataclass(frozen=True)
class Checklist:
    request: str
    context_json: str
    declarations_json: str
    criteria: tuple[Criterion, ...]

    @property
    def public_context(self) -> dict:
        return json.loads(self.context_json)

    @property
    def tool_declarations(self) -> list[dict]:
        return json.loads(self.declarations_json)

    def as_dict(self) -> dict:
        return {
            "request": self.request, "public_context": self.public_context,
            "tool_declarations": self.tool_declarations, "criteria": [asdict(row) for row in self.criteria],
        }


@dataclass(frozen=True)
class CriterionResult:
    id: str
    passed: bool | None
    why: str
    evidence: tuple[str, ...] = ()
    semantic_attempts: tuple[SemanticAttempt, ...] = ()


@dataclass(frozen=True)
class Review:
    criteria: tuple[CriterionResult, ...]
    blocking_error: str | None

    @property
    def continuous(self) -> float:
        return self._score.continuous

    @property
    def strict(self) -> float:
        return self._score.strict

    @property
    def _score(self):
        from .math import Verdict, score_trajectory

        return score_trajectory(
            tuple(Verdict(row.id, row.passed) for row in self.criteria),
            blocking_error=self.blocking_error is not None,
        )

    def as_dict(self) -> dict:
        return {
            "criteria": [asdict(row) for row in self.criteria],
            "blocking_error": self.blocking_error, "continuous": self.continuous, "strict": self.strict,
            "unresolved_criteria": [row.id for row in self.criteria if row.passed is None],
        }


def _text(value, maximum: int) -> bool:
    return (
        isinstance(value, str) and value == value.strip() and 0 < len(value) <= maximum
        and all(character.isprintable() for character in value)
    )


def _requested_page_size(request: str) -> int | None:
    match = re.search(r"\bpages of (three|[0-9]{1,2})\b", request, re.IGNORECASE)
    if match is None:
        return None
    value = match[1].casefold()
    return 3 if value == "three" else int(value)


def _fixed_checks(context: dict, request: str) -> list[Criterion]:
    checks = {
        "clean_execution": "The sandbox completed with a valid final response and no unresolved runtime error.",
        "valid_tool_use": "Tool calls used valid declared arguments and accessible entity identifiers.",
        "account_scope": "The reported account and invoice identifiers match the public account and assignment.",
        "invoice_coverage": "The rollout verifies the requested invoice set and accounts for every invoice in scope.",
    }
    if context["operation"] in {"review_invoices", "escalate"}:
        checks["assessment_coverage"] = "Every in-scope invoice has a decision supported by an assessment."
    if (size := _requested_page_size(request)) is not None:
        checks["page_size"] = f"Successful invoice search pages use the explicitly requested page size of {size}."
    if context["operation"] == "escalate":
        checks.update({
            "required_action_completion": "Every eligible authorized action required by the task persists and is verified by read-back.",
            "scoped_audit": "Every persisted action has an exact, authorized audit entry and no unrelated state changes.",
            "readback": "Every committed action is confirmed by a matching later read and reported accurately.",
            "idempotence": "Replays and retries do not create duplicate actions or audit entries.",
        })
    else:
        checks["read_only"] = "The read request leaves the sandbox database unchanged."
    return [Criterion(f"eoai_{hook}", description, hook) for hook, description in checks.items()]


def derive_checklist(
    request: str, public_context: dict, declarations: list[dict], complete: Complete,
) -> Checklist:
    """Freeze one checklist before execution, with an allowlist for all model inputs."""
    if (not isinstance(request, str) or not request.strip() or len(request) > 4096
            or set(public_context) != _CONTEXT_KEYS
            or public_context["operation"] not in {"list_invoices", "review_invoices", "escalate"}):
        raise ValueError("Invalid public EOAI review task")
    profile = declarations[0].get("schema_version") if declarations else None
    if profile not in {"v1", "v2"} or declarations != declarations_for_profile(f"enterprise_ops:{profile}"):
        raise ValueError("Checklist derivation requires the canonical public EOAI tool declarations")
    fixed = _fixed_checks(public_context, request)
    context_json = json.dumps(public_context, sort_keys=True)
    declarations_json = json.dumps(declarations, sort_keys=True)
    phrases = re.split(
        r"(?<=[.?!])\s+|,\s+|\s+and\s+(?=(?:use|follow|cite|flag|report|create|skip|ones)\b)",
        scrub(request), flags=re.IGNORECASE,
    )
    options = {f"s{index}": item for index, item in enumerate(
        dict.fromkeys(phrase.strip(" .,:;") for phrase in phrases if phrase.strip(" .,:;")), 1,
    )}
    payload = {
        "request": scrub(request), "public_context": scrub(json.loads(context_json)),
        "tool_declarations": [{
            "name": row["name"], "schema_version": row["schema_version"],
            "arguments": row["arguments"],
            "result_fields": sorted(row["result"].get("properties", {})),
        } for row in declarations],
        "deterministic_criteria": [row.description for row in fixed],
        "semantic_options": options,
    }
    try:
        result = complete("reviewer", "checklist", _CHECKLIST, payload, 0.0)
        criteria = _checklist_response(result, fixed, options)
    except ValueError:
        # A retry supplies format feedback only. No execution has taken place.
        correction = "\nList only IDs that occur in semantic_options and do not repeat an ID."
        result = complete("reviewer", "checklist", _CHECKLIST + correction, payload, 0.0)
        criteria = _checklist_response(result, fixed, options)
    return Checklist(request, context_json, declarations_json, criteria)


def _checklist_response(result: dict, fixed: list[Criterion], options: dict) -> tuple[Criterion, ...]:
    if type(result) is not dict or set(result) != {"selected"} or type(result["selected"]) is not list:
        raise ValueError("Invalid checklist response")
    selected = result["selected"]
    if any(type(key) is not str or key not in options for key in selected) or len(set(selected)) != len(selected):
        raise ValueError("Invalid semantic checklist criterion")
    ordered = [key for key in options if key in selected]
    return tuple(fixed) + tuple(Criterion(f"req_{key[1:]}", options[key]) for key in ordered)


@dataclass(frozen=True)
class _Call:
    ordinal: int
    name: str
    args: dict
    data: dict | None
    error: str | None
    call_id: str
    revision: int | None


@dataclass
class _Artifacts:
    tenant: str
    actor: str
    run_id: str
    sandbox_id: str
    stop: str
    output: dict
    calls: list[_Call]
    before: dict[str, tuple[dict, ...]]
    after: dict[str, tuple[dict, ...]]
    new_audits: list[dict]
    runtime_errors: tuple[str, ...]
    validation_errors: tuple[str, ...]


def _read_json(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("Review artifact exceeded its byte limit")
    value = strict_json(raw)
    if type(value) is not dict:
        raise ValueError("Expected a review artifact object")
    return value


def _database(path: Path) -> tuple[tuple[tuple, ...], dict[str, tuple[dict, ...]]]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        schema = tuple(tuple(row) for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ))
        tables = {}
        for kind, name, _, _ in schema:
            if kind != "table":
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Unexpected SQLite table name")
            # Quote the validated identifier; SQLite parameters cannot bind table names.
            rows = connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid')
            tables[name] = tuple(dict(row) for row in rows)
        return schema, tables


def _load_trace(path: Path, manifest: dict) -> tuple[list[_Call], tuple[str, ...], tuple[str, ...]]:
    calls = []
    runtime_errors = []
    validation_errors = []
    profile = TOOL_PROFILES[manifest["profile"]]
    total = 0
    with path.open("rb") as stream:
        for ordinal, raw in enumerate(stream, 1):
            total += len(raw)
            if total > 16_000_000:
                raise ValueError("Trace exceeded its byte limit")
            row = strict_json(raw)
            if row["ordinal"] != ordinal or row["as_of"] != manifest["as_of"]:
                raise ValueError("Trace event does not match rollout")
            if row["event"] == "error":
                runtime_errors.append(row["code"])
            if row["event"] != "tool":
                continue
            tool, arguments = row["tool"], row["arguments"]
            definition = TOOLS.get(tool)
            result_type = definition.result if definition else None
            envelope = (ToolResponse[result_type] if result_type else ToolResponse).model_validate(row["response"])
            meta = envelope.meta
            if (meta.profile != manifest["profile"] or meta.sandbox_id != manifest["sandbox_id"]
                    or meta.schema_version != profile.schema_version
                    or (definition and meta.tool_name != tool)
                    or (envelope.ok and (not definition or meta.state_revision is None))):
                raise ValueError("Tool envelope does not match rollout")
            error = envelope.error.code if envelope.error else None
            if not definition or error in _INVALID_ARGUMENTS:
                validation_errors.append(f"{tool} at trace.{ordinal}: {error or 'undeclared tool'}")
            if envelope.ok:
                argument_type = profile.argument_overrides.get(tool, definition.arguments)
                arguments = argument_type.model_validate(arguments).model_dump()
            calls.append(_Call(
                ordinal, tool, arguments,
                envelope.data.model_dump(mode="json") if envelope.data is not None else None,
                error, meta.call_id, meta.state_revision,
            ))
    if len({call.call_id for call in calls}) != len(calls):
        raise ValueError("Duplicate tool call ID in trace")
    return calls, tuple(runtime_errors), tuple(validation_errors)


def _added(before: tuple[dict, ...], after: tuple[dict, ...], keys: tuple[str, ...]) -> list[dict]:
    original = {tuple(row[key] for key in keys) for row in before}
    return [row for row in after if tuple(row[key] for key in keys) not in original]


def _load(checklist: Checklist, trial, snapshot_path: Path, task: RunTask | None) -> _Artifacts:
    trace = trial.trace_path
    manifest = _read_json(trace / "manifest.json")
    inputs = _read_json(trace / "input.json")
    summary = _read_json(trace / "summary.json")
    output = RunOutput.model_validate(_read_json(trace / "final_output.json")).model_dump(mode="json")
    if (inputs["request"] != scrub(checklist.request) or inputs["task"] != scrub(checklist.public_context)
            or declarations_for_profile(manifest["profile"]) != checklist.tool_declarations
            or output["run_id"] != manifest["run_id"] or output["task_id"] != manifest["task_id"]
            or output["as_of"] != manifest["as_of"] or output["status"] != summary["status"]):
        raise ValueError("Rollout does not match the frozen review input")
    if task is not None:
        scope_digest = task.write_scope.digest if task.write_scope else None
        if (task_digest(task) != manifest["task_digest"] or scope_digest != manifest["write_scope_digest"]
                or task.principal.tenant_id != inputs["tenant_id"] or task.principal.user_id != inputs["user_id"]):
            raise ValueError("Trusted task and write scope differ from recorded run")
    with snapshot_path.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != manifest["snapshot_digest"]:
            raise ValueError("Verification snapshot differs from recorded seed")
    if checklist.public_context["authorized_actions"] and task is None:
        raise ValueError("Authorized review requires the trusted task and write scope")
    calls, errors, validation = _load_trace(trace / "trace.jsonl", manifest)
    if len(calls) != summary["tool_calls"]:
        raise ValueError("Rollout tool count does not match trace")
    old_schema, before = _database(snapshot_path)
    new_schema, after = _database(trial.sandbox.path)
    if old_schema != new_schema or not set(_MUTABLE_TABLES) <= before.keys():
        raise ValueError("Rollout database schema does not match its seed")
    return _Artifacts(
        tenant=inputs["tenant_id"], actor=inputs["user_id"], run_id=manifest["run_id"],
        sandbox_id=manifest["sandbox_id"], stop=summary["stop_reason"], output=output, calls=calls,
        before=before, after=after, new_audits=_added(before["audit_events"], after["audit_events"], ("tenant_id", "id")),
        runtime_errors=errors, validation_errors=validation,
    )


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _scope(checklist: Checklist, artifacts: _Artifacts) -> tuple[str | None, set[str], bool]:
    context = checklist.public_context
    customers = [row for row in artifacts.before["customers"] if row["tenant_id"] == artifacts.tenant]
    customer = context["customer_id"]
    if customer is not None:
        known = any(row["id"] == customer for row in customers)
    elif context["customer_query"]:
        query = _normalize_name(context["customer_query"])
        exact = [row["id"] for row in customers if _normalize_name(row["legal_name"]) == query]
        customer = exact[0] if len(exact) == 1 else None
        known = True
    else:
        known = False
    invoices = {
        row["id"] for row in artifacts.before["invoices"]
        if row["tenant_id"] == artifacts.tenant and row["customer_id"] == customer
    }
    if context["invoice_ids"] is not None:
        invoices.intersection_update(context["invoice_ids"])
    return customer, invoices, known


def _pagination(calls: list[_Call], ids: list[str], revision: int | None = None) -> bool:
    """Check a complete page chain against the ordered public records in SQLite."""
    chains = {}
    complete = False
    for call in calls:
        if revision is not None and call.revision != revision:
            continue
        key = (call.revision, json.dumps({k: v for k, v in call.args.items() if k != "cursor"}, sort_keys=True))
        offset = 0 if call.args["cursor"] is None else chains.pop(key, None)
        if offset is None:
            continue
        limit = call.args["limit"]
        records = call.data["items"]
        cursor = call.data["next_cursor"]
        if [item["id"] for item in records] != ids[offset:offset + limit]:
            continue
        end = offset + len(records)
        if cursor is None:
            complete |= end == len(ids)
        elif end < len(ids):
            chains[key] = end
    return complete


def _customer_search(checklist: Checklist, artifacts: _Artifacts) -> bool:
    """Check identity evidence for accounts identified by name and for ambiguity."""
    context = checklist.public_context
    if context["customer_id"] or not context["customer_query"]:
        return True
    query = _normalize_name(context["customer_query"])
    aliases = {
        row["customer_id"] for row in artifacts.before["customer_aliases"]
        if row["tenant_id"] == artifacts.tenant and row["normalized_alias"] == query
    }
    expected = sorted(
        row["id"] for row in artifacts.before["customers"] if row["tenant_id"] == artifacts.tenant
        and (query in _normalize_name(row["legal_name"]) or row["id"] in aliases)
    )
    calls = [
        call for call in artifacts.calls if call.name == "customer.search" and call.data is not None
        and _normalize_name(call.args["query"]) == query
    ]
    return _pagination(calls, expected)


def _scope_result(checklist: Checklist, artifacts: _Artifacts, customer: str | None, invoices: set[str],
                  known: bool) -> bool:
    output = artifacts.output
    coverage = output["coverage"]
    observed = set(coverage["observed_invoice_ids"])
    mentioned = observed | {row["invoice_id"] for row in output["decisions"] + output["actions"]}
    if not known or customer != coverage["customer_id"] or not mentioned <= invoices:
        return False
    if not _customer_search(checklist, artifacts):
        return False
    if customer is None:
        codes = {row["code"] for row in output["missing_information"]}
        return output["review_required"] and bool(codes & {
            "AMBIGUOUS_CUSTOMER", "CUSTOMER_NOT_FOUND", "CUSTOMER_UNRESOLVED",
        })
    return len(coverage["observed_invoice_ids"]) == len(observed)


def _invoice_coverage(checklist: Checklist, artifacts: _Artifacts, customer: str | None, invoices: set[str],
                      scoped: bool) -> bool:
    coverage = artifacts.output["coverage"]
    if customer is None:
        return scoped and not coverage["observed_invoice_ids"]
    if set(coverage["observed_invoice_ids"]) != invoices or coverage["customer_id"] != customer:
        return False
    search = [
        call for call in artifacts.calls if call.name == "invoice.search" and call.data is not None
        and call.args["customer_id"] == customer
    ]
    assigned = checklist.public_context["invoice_ids"]
    if assigned is None:
        return coverage["invoice_search_exhausted"] and _pagination(
            [call for call in search if call.args["statuses"] is None],
            sorted(invoices), coverage["state_revision"],
        )
    if invoices != set(assigned):
        return False
    observed = {item["id"] for call in search for item in call.data["items"] if item["customer_id"] == customer}
    observed.update(
        call.data["id"] for call in artifacts.calls if call.name == "invoice.get" and call.data is not None
        and call.data["customer_id"] == customer and call.data["id"] == call.args["invoice_id"]
    )
    observed.update(
        call.data["invoice_id"] for call in artifacts.calls
        if call.name in {"ticket.preview_create", *_ACTION_TABLES} and call.data is not None
    )
    return invoices <= observed


def _assessment_coverage(artifacts: _Artifacts, invoices: set[str]) -> bool:
    decisions = artifacts.output["decisions"]
    if {row["invoice_id"] for row in decisions} != invoices or len(decisions) != len(invoices):
        return False
    evidence = {row["id"]: row for row in artifacts.output["evidence"]}
    assessments = {
        call.call_id: call.data for call in artifacts.calls
        if call.name == "invoice.assess" and call.data is not None
        and call.args["invoice_id"] == call.data["invoice_id"]
    }
    for decision in decisions:
        observed = [
            assessments[row["observed_call_id"]]
            for identifier in decision["evidence_ids"]
            if (row := evidence.get(identifier)) and row["source_type"] == "invoice_assessment"
            and row["source_id"] == decision["invoice_id"] and row["observed_call_id"] in assessments
        ]
        if not any(
            (row["invoice_id"], row["status"], row["reason_codes"], row["effective_due_date"],
             row["days_past_due"], row["outstanding_minor"], row["currency"])
            == (decision["invoice_id"], decision["decision"], decision["reason_codes"], decision["effective_due_date"],
                decision["days_past_due"], decision["outstanding_minor"], decision["currency"])
            for row in observed
        ):
            return False
    return True


def _audit_after(row: dict) -> dict:
    value = strict_json(row["after_json"])
    if not isinstance(value, dict):
        raise ValueError("Invalid audit after-state")
    return value


def _fingerprints(rows: tuple[dict, ...]) -> Counter:
    return Counter(tuple(row.items()) for row in rows)


def _allowed_revisions(artifacts: _Artifacts) -> bool:
    original = {row["id"]: row for row in artifacts.before["tenants"]}
    current = {row["id"]: row for row in artifacts.after["tenants"]}
    if artifacts.tenant not in original or original.keys() != current.keys():
        return False
    revision = original[artifacts.tenant]["revision"]
    expected = original | {
        artifacts.tenant: original[artifacts.tenant] | {"revision": revision + len(artifacts.new_audits)},
    }
    return current == expected and sorted(row["state_revision"] for row in artifacts.new_audits) == list(
        range(revision + 1, revision + len(artifacts.new_audits) + 1)
    )


def _audit_matches_row(audit: dict, entity: dict, artifacts: _Artifacts) -> bool:
    record = _audit_after(audit)
    if (audit["before_json"] is not None or record.get("id") != entity["id"]
            or record.get("invoice_id") != entity["invoice_id"] or record.get("status") != entity["status"]):
        return False
    if audit["action"] == "ticket.create":
        return (set(record) == {"id", "invoice_id", "status", "policy_id", "policy_version", "approval_id"}
                and record["policy_id"] == entity["policy_id"] and record["policy_version"] == entity["policy_version"]
                and entity["created_by"] == artifacts.actor and entity["created_at"] == audit["observed_at"]
                and entity["reason_code"] == audit["reason_code"]
                and (record["approval_id"] is None or any(
                    row["tenant_id"] == artifacts.tenant and row["id"] == record["approval_id"]
                    and row["consumed_ticket_id"] == entity["id"] for row in artifacts.after["approval_requests"]
                )))
    return (set(record) == {"id", "invoice_id", "status", "expires_at"}
            and record["expires_at"] == entity["expires_at"] and entity["creation_audit_id"] == audit["id"]
            and entity["requested_by"] == artifacts.actor and entity["run_id"] == artifacts.run_id
            and entity["sandbox_id"] == artifacts.sandbox_id and entity["created_at"] == audit["observed_at"])


def _scoped_audit(checklist: Checklist, artifacts: _Artifacts, invoices: set[str]) -> bool:
    before, after = artifacts.before, artifacts.after
    if any(before[name] != after[name] for name in before.keys() - _MUTABLE_TABLES):
        return False
    if not _allowed_revisions(artifacts):
        return False
    for name in ("audit_events", "escalation_tickets", "command_receipts"):
        if _fingerprints(before[name]) - _fingerprints(after[name]):
            return False
    approvals = {(row["tenant_id"], row["id"]): row for row in after["approval_requests"]}
    consumed = {
        (audit["tenant_id"], record.get("approval_id")): audit["subject_id"]
        for audit in artifacts.new_audits if audit["action"] == "ticket.create"
        and (record := _audit_after(audit)).get("approval_id") is not None
    }
    for original in before["approval_requests"]:
        key = original["tenant_id"], original["id"]
        expected = original if key not in consumed else original | {
            "consumed_ticket_id": consumed[key], "version": original["version"] + 1,
        }
        if approvals.get(key) != expected:
            return False
    actions = set(checklist.public_context["authorized_actions"])
    permitted = {
        tool for tool in _ACTION_TABLES if TOOLS[tool].capability.value in actions
    }
    matching = {}
    calls = {call.call_id: call for call in artifacts.calls}
    for tool, table in _ACTION_TABLES.items():
        for row in _added(before[table], after[table], ("tenant_id", "id")):
            matching[row["tenant_id"], tool, row["id"]] = row
    receipts = _added(
        before["command_receipts"], after["command_receipts"],
        ("tenant_id", "sandbox_id", "run_id", "actor_id", "action", "command_key"),
    )
    audit_ids = set()
    for audit in artifacts.new_audits:
        key = audit["tenant_id"], audit["action"], audit["subject_id"]
        entity = matching.pop(key, None)
        call = calls.get(audit["call_id"])
        if (entity is None or audit["tenant_id"] != artifacts.tenant or entity["invoice_id"] not in invoices
                or audit["run_id"] != artifacts.run_id or audit["actor_id"] != artifacts.actor
                or audit["action"] not in permitted or call is None or call.name != audit["action"]
                or not _audit_matches_row(audit, entity, artifacts)):
            return False
        audit_ids.add((audit["id"], audit["action"], entity["invoice_id"], audit["subject_id"]))
    receipt_ids = {
        (row["audit_id"], row["action"], row["invoice_id"], row["result_id"]) for row in receipts
        if row["tenant_id"] == artifacts.tenant and row["sandbox_id"] == artifacts.sandbox_id
        and row["run_id"] == artifacts.run_id and row["actor_id"] == artifacts.actor
    }
    return not matching and audit_ids == receipt_ids and all(
        row["tenant_id"] == artifacts.tenant and row["sandbox_id"] == artifacts.sandbox_id
        and row["run_id"] == artifacts.run_id and row["actor_id"] == artifacts.actor
        for row in receipts
    )


def _readback(artifacts: _Artifacts) -> bool:
    """Reconcile persisted actions, gateway receipts, later reads, and reported actions."""
    calls = [call for call in artifacts.calls if call.data is not None]
    output_actions = artifacts.output["actions"]
    if any(row["status"] in {"committed_unverified", "commit_unknown"} for row in output_actions):
        return False
    persisted = {
        (row["id"], row["action"], row["subject_id"], row["call_id"], row["state_revision"],
         _audit_after(row).get("invoice_id")): row
        for row in artifacts.new_audits if row["action"] in _READBACK
    }
    observed = set()
    confirmed = set()
    for call in calls:
        if call.name not in _READBACK:
            continue
        row = call.data
        key = (row["audit_id"], call.name, row["result_id"], row["creating_call_id"], row["state_revision"],
               row["invoice_id"])
        if key not in persisted or (not row["replayed"] and row["creating_call_id"] != call.call_id):
            return False
        observed.add(key)
        for read in calls:
            if (read.ordinal > call.ordinal and read.name == _READBACK[call.name]
                    and read.data["id"] == row["result_id"] and read.data["invoice_id"] == row["invoice_id"]
                    and read.data["creation_audit_id"] == row["audit_id"]
                    and read.data["creating_call_id"] == row["creating_call_id"]
                    and read.revision >= row["state_revision"]
                    and read.data["status"] == _audit_after(persisted[key])["status"]):
                confirmed.add(key)
                break
    reported = [
        (row["audit_id"], row["type"], row["result_id"], row["invoice_id"], row["status"])
        for row in output_actions if row["confirmed_by_readback"]
    ]
    expected = {
        (key[0], ACTION_TYPES[key[1]], key[2], key[5], "completed" if key[1] == "ticket.create" else "pending_approval")
        for key in confirmed
    }
    return (set(persisted) == observed == confirmed and len(reported) == len(expected) and set(reported) == expected
            and all(row["confirmed_by_readback"] for row in output_actions
                    if row["status"] in {"completed", "pending_approval"}))


def _idempotent(artifacts: _Artifacts) -> bool:
    actions = [
        (row["tenant_id"], row["action"], _audit_after(row).get("invoice_id"))
        for row in artifacts.new_audits if row["action"] in _ACTION_TABLES
    ]
    active = [
        (row["tenant_id"], row["invoice_id"])
        for row in artifacts.after["escalation_tickets"] if row["status"] in {"open", "in_progress"}
    ]
    return (len(actions) == len(set(actions)) and len(active) == len(set(active))
            and all(invoice is not None for _, _, invoice in actions))


def _blocking_error(artifacts: _Artifacts) -> str | None:
    if artifacts.stop != "model_final":
        return f"Rollout stopped with {artifacts.stop}."
    if artifacts.output["status"] == "failed":
        return "The runtime produced a failed output."
    unresolved = {}
    for call in artifacts.calls:
        if call.error in {"UNKNOWN_ERROR", "TRANSIENT_FAILURE", "RATE_LIMITED"}:
            unresolved[call.name] = call.error
        elif call.error is None:
            unresolved.pop(call.name, None)
    if unresolved:
        return "An unresolved tool runtime error blocked verification."
    return None


def _required_actions(
    task: RunTask | None, seed: Path, artifacts: _Artifacts, invoices: set[str],
    audit_valid: bool, readback_valid: bool,
) -> tuple[bool, str]:
    """Derive obligations from the authorized assignment and pre-run public policy state."""
    scope = task.write_scope if task else None
    required = set()
    existing = {
        row["invoice_id"] for row in artifacts.before["escalation_tickets"]
        if row["tenant_id"] == artifacts.tenant and row["status"] in {"open", "in_progress"}
    }
    if scope is not None:
        clock = FrozenClock(datetime.fromisoformat(artifacts.output["as_of"]))
        with closing(sqlite3.connect(seed.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            permitted = set()
            for capability in scope.capabilities:
                try:
                    authorize(connection, scope.principal, capability)
                except Forbidden:
                    continue
                permitted.add(capability)
            for invoice in invoices - existing:
                assessment = load_assessment(connection, artifacts.tenant, invoice, clock, scope.restrictions)
                if assessment.status != "eligible":
                    continue
                if assessment.requires_approval and Capability.REQUEST_APPROVAL in permitted:
                    required.add(("approval.request", invoice))
                if not assessment.requires_approval and Capability.CREATE_TICKET in permitted:
                    required.add(("ticket.create", invoice))

    observed = {
        (row["action"], _audit_after(row).get("invoice_id"))
        for row in artifacts.new_audits if row["action"] in _ACTION_TABLES
    }
    denied = any(call.name in _ACTION_TABLES and call.error == "PERMISSION_DENIED" for call in artifacts.calls)
    if denied or observed - required:
        return False, "Unauthorized or unrequired write attempted or persisted."
    missing = required - observed
    if missing:
        omitted = ", ".join(f"{action} for {invoice}" for action, invoice in sorted(missing))
        return False, f"Missing required persisted action: {omitted}."
    if not audit_valid or not readback_valid:
        return False, "Write evidence lacks verified persistence, matching receipt or later read-back."
    if not required:
        return True, "No new authorized action was required by the task and seed state."
    return True, f"Verified {len(required)} required persisted action(s) by receipt and read-back."


def _deterministic(checklist: Checklist, artifacts: _Artifacts, blocking: str | None,
                   seed: Path, task: RunTask | None) -> dict:
    customer, invoices, known = _scope(checklist, artifacts)
    scoped = _scope_result(checklist, artifacts, customer, invoices, known)
    covered = _invoice_coverage(checklist, artifacts, customer, invoices, scoped)
    coverage = artifacts.output["coverage"]
    page_calls = [call for call in artifacts.calls if call.data is not None and
                  call.name in {"customer.search", "invoice.search"}]
    continuations = "; ".join(
        f"{call.name} limit={call.args['limit']} returned a continuation at trace.{call.ordinal}"
        for call in page_calls[-6:] if call.data["next_cursor"] is not None
    )
    checks = {
        "clean_execution": (blocking is None, blocking or "The runtime returned a valid final response."),
        "valid_tool_use": (
            not artifacts.validation_errors,
            "; ".join(artifacts.validation_errors[:3]) or "Tool names and arguments match the active public schema.",
        ),
        "account_scope": (
            scoped, "Account identity and invoice scope verified." if scoped else
            f"Account or invoice scope did not reconcile. {continuations or 'No successful continuation evidence.'}",
        ),
        "invoice_coverage": (
            covered, "Invoice coverage reconciles with the seed and page chain." if covered else
            f"Recorded {len(coverage['observed_invoice_ids'])} of {len(invoices)} scoped invoices. "
            f"Invoice search exhausted: {coverage['invoice_search_exhausted']}. {continuations}",
        ),
    }
    operation = checklist.public_context["operation"]
    if (size := _requested_page_size(checklist.request)) is not None:
        pages = [call for call in artifacts.calls if call.name == "invoice.search" and call.data is not None]
        observed_sizes = sorted({call.args["limit"] for call in pages})
        checks["page_size"] = (
            observed_sizes == [size], f"Required page size {size}. "
            f"Successful invoice searches used {observed_sizes or 'no observed pages'}.",
        )
    if operation in {"review_invoices", "escalate"}:
        assessed = _assessment_coverage(artifacts, invoices)
        missing = invoices - {row["invoice_id"] for row in artifacts.output["decisions"]}
        checks["assessment_coverage"] = (
            assessed, "All scoped decisions reconcile with observed assessments." if assessed else
            f"{len(missing)} scoped invoice decisions missing."
            if missing else "A reported decision lacks a matching observed assessment.",
        )
    if operation == "escalate":
        audit_valid = _scoped_audit(checklist, artifacts, invoices)
        readback_valid = _readback(artifacts)
        idempotent = _idempotent(artifacts)
        checks.update({
            "scoped_audit": (
                audit_valid, "Persisted writes match audit and receipts within authorized scope." if audit_valid
                else "A persisted write, audit, receipt, scope or database revision did not reconcile.",
            ),
            "readback": (readback_valid, "Persisted writes match later read-back and output." if readback_valid
                         else "A write lacked matching later read-back or accurate output evidence."),
            "idempotence": (idempotent, "No duplicate actions or active tickets." if idempotent
                            else "Duplicate action audits or active tickets found."),
            "required_action_completion": _required_actions(
                task, seed, artifacts, invoices, audit_valid and idempotent, readback_valid,
            ),
        })
    else:
        changed = sorted(name for name in artifacts.before if artifacts.before[name] != artifacts.after[name])
        checks["read_only"] = (
            not changed, f"Read-only task changed database tables: {', '.join(changed)}." if changed
            else "All database rows match the seed.",
        )
    return checks


def _shorten(value):
    if isinstance(value, str):
        return value if len(value) <= 600 else value[:600] + "…"
    if isinstance(value, list):
        return [_shorten(item) for item in value[:25]]
    if isinstance(value, dict):
        return {key: _shorten(item) for key, item in value.items()}
    return value


def _evidence(artifacts: _Artifacts) -> dict:
    """Only pass a bounded projection of public rollout facts to the semantic reviewer."""
    output = artifacts.output
    entries = [
        ("output.status", {"status": output["status"], "review_required": output["review_required"],
                           "summary": output["summary"], "business_date": output["business_date"]}),
        ("output.coverage", output["coverage"]),
    ]
    for field in ("actions", "decisions", "missing_information"):
        entries.extend((f"output.{field}.{index}", value) for index, value in enumerate(output[field]))
    referenced = {identifier for row in output["decisions"] + output["actions"]
                  for identifier in row["evidence_ids"]}
    citations = [row for row in output["evidence"] if row["id"] in referenced]
    citations.sort(key=lambda row: row["source_type"] != "document_span")
    entries.extend((f"output.citation.{index}", row) for index, row in enumerate(citations[:24]))
    for row in artifacts.new_audits:
        entries.append((f"audit.{row['id']}", {
            "action": row["action"], "subject_id": row["subject_id"], "call_id": row["call_id"],
            "reason_code": row["reason_code"], "state_revision": row["state_revision"], "after": _audit_after(row),
        }))
    for call in artifacts.calls:
        data = call.data
        if data is not None and "items" in data:
            items = data["items"]
            data = {"items": [{key: row[key] for key in (
                "id", "invoice_id", "customer_id", "legal_name", "status", "match_kind"
            ) if key in row} for row in items], "has_next_page": data["next_cursor"] is not None}
        public_arguments = {
            key: value for key, value in call.args.items()
            if key not in {"cursor", "page_cursor", "preview_token", "idempotency_key"}
        }
        if data is not None:
            data = {key: value for key, value in data.items() if key not in {"preview_token", "idempotency_key"}}
        entries.append((f"trace.{call.ordinal}", {
            "tool": call.name, "arguments": public_arguments, "call_id": call.call_id,
            "state_revision": call.revision, "result": data, "error": call.error,
        }))
    evidence = {}
    size = 2
    for key, raw in entries:
        entry = _shorten(scrub(raw))
        serialized = len(json.dumps({key: entry}, ensure_ascii=True))
        if serialized <= 3_072 and size + serialized <= 24_000:
            evidence[key] = entry
            size += serialized
    return evidence


def _semantic(checklist: Checklist, criterion: Criterion, evidence: dict, complete: Complete) -> CriterionResult:
    grade = grade_criterion({
        "request": scrub(checklist.request), "public_context": scrub(checklist.public_context),
        "tool_names": [row["name"] for row in checklist.tool_declarations],
        "criterion": {"id": criterion.id, "description": criterion.description}, "evidence": evidence,
    }, complete)
    return CriterionResult(criterion.id, grade.passed, grade.why, grade.evidence, grade.attempts)


def review_rollout(checklist: Checklist, trial, snapshot_path: Path, complete: Complete, *,
                   task: RunTask | None = None) -> Review:
    """Grade the same frozen checklist against one isolated rollout and its SQLite state."""
    try:
        artifacts = _load(checklist, trial, snapshot_path, task)
        blocking = _blocking_error(artifacts)
        checks = _deterministic(checklist, artifacts, blocking, snapshot_path, task)
        evidence = _evidence(artifacts)
    except (OSError, sqlite3.Error, KeyError, TypeError, AttributeError, ValueError, ValidationError, RecursionError) as error:
        raise ValueError("The rollout did not produce verifiable sandbox artifacts") from error
    results = []
    for row in checklist.criteria:
        if row.hook is None:
            results.append(_semantic(checklist, row, evidence, complete))
        else:
            results.append(CriterionResult(row.id, *checks[row.hook]))
    return Review(tuple(results), blocking)
