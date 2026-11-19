import json
from contextlib import closing
from dataclasses import replace

import pytest

from enterprise_ops.agent.model import ModelError, ModelTurn, ToolCall
from enterprise_ops.agent.runner import RunLimits, RunTask, run_task
from enterprise_ops.domain.entities import Capability, Principal
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, seed_snapshot
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools.session import WriteScope


PRINCIPAL = Principal("northstar_property", "analyst")


class ScriptModel:
    provider = "script"
    model_id = "scripted-workflow"

    def __init__(self, steps):
        self.steps = iter(steps)
        self.index = 0

    def turn(self, *, instructions, transcript, declarations, timeout, max_output_tokens):
        assert timeout > 0 and max_output_tokens > 0
        assert len(declarations) == 18
        assert "evidence" in instructions
        self.index += 1
        step = next(self.steps)
        if isinstance(step, ModelError):
            raise step
        if isinstance(step, str):
            return ModelTurn(final=step, input_tokens=10, output_tokens=5)
        name, args = step
        if callable(args):
            last = transcript[-1]
            context = json.loads(last["content"]) if last.get("role") == "user" else json.loads(last["output"])["data"]
            args = args(context)
        return ModelTurn(
            calls=(ToolCall(f"tool-{self.index}", name, json.dumps(args)),),
            input_tokens=10, output_tokens=5,
        )


def _assignment():
    scope = WriteScope(
        PRINCIPAL, "signed-request", "acme", frozenset({Capability.CREATE_TICKET}),
        invoice_ids=frozenset({"INV-104"}),
    )
    return RunTask(
        "acme-escalation", "Escalate INV-104 if eligible.", PRINCIPAL, "escalate", write_scope=scope,
    )


def _preview(assessment):
    return {
        "invoice_id": assessment["invoice_id"], "reason_code": "OVERDUE_WITH_OUTSTANDING_BALANCE",
        "evidence_ids": assessment["evidence_ids"],
    }


def _commit(preview):
    return {
        "preview_token": preview["preview_token"], "idempotency_key": preview["idempotency_key"],
        "expected_tenant_revision": preview["tenant_revision"],
        "expected_invoice_version": preview["invoice_version"],
        "expected_policy_version": preview["policy_version"],
    }


def _run(path, task, steps, **kwargs):
    snapshot = seed_snapshot(path / "snapshots", "baseline")
    return run_task(
        snapshot, task, ScriptModel(steps), runs_root=path / "runs", run_id="smoke", **kwargs,
    )


def test_run_verifies_ticket_before_claiming_completion(tmp_path):
    result = _run(tmp_path, _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        ("ticket.create", _commit),
        '{"customer_id":"acme","review_reason":null}',
    ])
    output = result.output
    assert output.status == "resolved"
    assert output.decisions[0].outstanding_minor == 1_000_000
    action, = output.actions
    assert action.status == "completed" and action.confirmed_by_readback
    assert set(action.evidence_ids) <= {entry.id for entry in output.evidence}
    events = [json.loads(line) for line in (result.trace_path / "trace.jsonl").read_text().splitlines()]
    tool_events = [event for event in events if event["event"] == "tool"]
    assert [event["tool"] for event in tool_events][-2:] == ["ticket.create", "ticket.get"]
    assert tool_events[-2]["arguments"]["idempotency_key"] == "[redacted]"
    assert tool_events[-2]["arguments"]["preview_token"] == "[redacted]"
    delta = json.loads((result.trace_path / "state_delta.json").read_text())
    assert [event["id"] for event in delta] == [action.audit_id]
    final = json.loads((result.trace_path / "final_output.json").read_text())
    assert final == output.model_dump(mode="json")


def test_list_requires_an_exhausted_page_chain(tmp_path):
    task = RunTask(
        "acme-invoices", "Find invoices for Acme Property Group.", PRINCIPAL, "list_invoices",
        customer_query="Acme Property Group",
    )
    result = _run(tmp_path, task, [
        ("customer.search", {"query": "Acme Property Group"}),
        ("invoice.search", {"customer_id": "acme", "limit": 2}),
        ("invoice.search", lambda data: {"customer_id": "acme", "limit": 2, "cursor": data["next_cursor"]}),
        '{"customer_id":"acme","review_reason":null}',
    ])
    assert result.output.status == "resolved"
    assert result.output.coverage.invoice_search_exhausted
    assert result.output.coverage.observed_invoice_ids == ["INV-104", "INV-105", "INV-106", "INV-107"]


def test_conflicting_amendments_return_review_without_mutation(tmp_path):
    task = RunTask(
        "conflict", "Assess disputed contract terms for INV-M01.", PRINCIPAL, "review_invoices",
        customer_id="meridian", invoice_ids=frozenset({"INV-M01"}),
    )
    result = _run(tmp_path, task, [
        ("invoice.get", {"invoice_id": "INV-M01"}),
        ("invoice.assess", {"invoice_id": "INV-M01"}),
        '{"customer_id":"meridian","review_reason":null}',
    ])
    assert result.output.review_required
    assert result.output.decisions[0].decision == "needs_review"
    assert "AMBIGUOUS_AMENDMENT" in result.output.decisions[0].reason_codes
    assert json.loads((result.trace_path / "state_delta.json").read_text()) == []


def test_reserve_readback_before_dispatching_write(tmp_path):
    result = _run(tmp_path, _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        ("ticket.create", _commit),
    ], limits=RunLimits(tool_calls=4))
    assert "completed" not in {action.status for action in result.output.actions}
    assert json.loads((result.trace_path / "summary.json").read_text())["stop_reason"] == "tool_budget"
    with closing(connect(result.sandbox.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


def test_provider_failure_keeps_observed_facts_and_scrubs_trace(tmp_path):
    task = RunTask(
        "read-error", "Check invoices. sk-abcdef1234567890", PRINCIPAL, "review_invoices", customer_id="acme",
    )
    result = _run(tmp_path, task, [
        ("invoice.search", {"customer_id": "acme", "limit": 1}),
        ModelError("Bearer secret012345"),
    ])
    assert result.output.status == "partial"
    assert result.output.coverage.observed_invoice_ids == ["INV-104"]
    assert not result.output.coverage.invoice_search_exhausted
    artifacts = "".join(path.read_text() for path in result.trace_path.iterdir())
    assert "secret012345" not in artifacts and "sk-abcdef1234567890" not in artifacts


def test_rejected_tool_arguments_cannot_leak_preview_or_break_trace(tmp_path):
    sensitive = {}

    def corrupt(preview):
        sensitive.update(preview)
        return {
            "invoice_id": preview["preview_token"] + "\ud800",
            "unexpected": preview["idempotency_key"],
        }

    result = _run(tmp_path, _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        ("invoice.get", corrupt),
        '{"customer_id":"acme","review_reason":null}',
        '{"customer_id":"acme","review_reason":null}',
    ])
    trace = (result.trace_path / "trace.jsonl").read_text()
    assert sensitive["preview_token"] not in trace
    assert sensitive["idempotency_key"] not in trace
    assert "unexpected" not in trace
    assert result.output.status == "partial"
    assert (result.trace_path / "final_output.json").is_file()


@pytest.mark.parametrize("invalid_id", ["x" * 129, chr(0xD800)])
def test_invalid_task_id_fails_before_any_write(invalid_id):
    with pytest.raises(ValueError, match="Task ID"):
        replace(_assignment(), id=invalid_id)


def test_ready_preview_gets_one_followup_and_still_requires_a_model_tool_call(tmp_path):
    result = _run(tmp_path, _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        '{"customer_id":"acme","review_reason":"null"}',
        ("ticket.create", lambda context: context["arguments"]),
        '{"customer_id":"acme","review_reason":null}',
    ])
    assert result.output.status == "resolved"
    assert [action.status for action in result.output.actions] == ["completed"]
    events = [json.loads(line)["event"] for line in (result.trace_path / "trace.jsonl").read_text().splitlines()]
    assert events.count("action_followup") == 1


def test_confirmed_preview_uses_the_exact_gateway_command(tmp_path):
    result = _run(tmp_path, _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        '{"customer_id":"acme","review_reason":null}',
        '{"submit_preview":true}',
        '{"customer_id":"acme","review_reason":null}',
    ])
    assert result.output.status == "resolved"
    assert [action.status for action in result.output.actions] == ["completed"]


def test_unsolicited_submit_signal_and_decline_do_not_mutate(tmp_path):
    unsolicited = _run(tmp_path / "unsolicited", _assignment(), [
        '{"submit_preview":true}',
        '{"customer_id":"acme","review_reason":"MISSING_INFORMATION"}',
    ])
    assert json.loads((unsolicited.trace_path / "state_delta.json").read_text()) == []
    declined = _run(tmp_path / "declined", _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        '{"customer_id":"acme","review_reason":null}',
        '{"submit_preview":false}',
    ])
    assert declined.output.status == "partial"
    assert json.loads((declined.trace_path / "state_delta.json").read_text()) == []


def test_runtime_retries_exact_lost_command_and_read_back(tmp_path):
    faults = FaultPlan((
        FaultStep("invoice.get", 1, FaultKind.RATE_LIMITED),
        FaultStep("ticket.create", 1, FaultKind.LOSE_RESPONSE),
        FaultStep("ticket.get", 1, FaultKind.TRANSIENT_FAILURE),
    ))
    result = _run(tmp_path, _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        ("ticket.create", _commit),
        '{"customer_id":"acme","review_reason":null}',
    ], faults=faults)
    action, = result.output.actions
    assert action.status == "completed" and action.confirmed_by_readback
    events = [json.loads(line) for line in (result.trace_path / "trace.jsonl").read_text().splitlines()]
    assert [event["tool"] for event in events if event["event"] == "retry"] == [
        "invoice.get", "ticket.create", "ticket.get",
    ]
    assert len(json.loads((result.trace_path / "state_delta.json").read_text())) == 1


def test_budget_stops_exact_retry_and_surfaces_unknown_write(tmp_path):
    faults = FaultPlan((FaultStep("ticket.create", 1, FaultKind.LOSE_RESPONSE),))
    result = _run(tmp_path, _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        ("ticket.create", _commit),
        '{"customer_id":"acme","review_reason":null}',
    ], faults=faults, limits=RunLimits(tool_calls=5))
    assert result.output.status == "partial"
    assert any(action.status == "commit_unknown" for action in result.output.actions)
    assert len(json.loads((result.trace_path / "state_delta.json").read_text())) == 1


def test_repeated_invalid_tool_call_stops_before_third_attempt(tmp_path):
    task = RunTask("wrong-schema", "Find the invoices.", PRINCIPAL, "list_invoices", customer_id="acme")
    result = _run(tmp_path, task, [
        ("invoice.search", {"customer_id": "acme", "limit": 999}),
        ("invoice.search", {"customer_id": "acme", "limit": 999}),
    ])
    summary = json.loads((result.trace_path / "summary.json").read_text())
    assert summary["stop_reason"] == "invalid_tool"
    assert summary["tool_calls"] == 2


def test_unreconciled_write_never_returns_to_model_for_another_key(tmp_path):
    faults = FaultPlan(tuple(
        FaultStep("ticket.create", attempt, FaultKind.LOSE_RESPONSE) for attempt in (1, 2, 3)
    ))
    result = _run(tmp_path, _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        ("ticket.create", _commit),
    ], faults=faults)
    events = [json.loads(line) for line in (result.trace_path / "trace.jsonl").read_text().splitlines()]
    assert sum(event["event"] == "tool" and event["tool"] == "ticket.create" for event in events) == 3
    assert json.loads((result.trace_path / "summary.json").read_text())["stop_reason"] == "commit_unknown"
    assert [action.status for action in result.output.actions] == ["commit_unknown"]
    assert len(json.loads((result.trace_path / "state_delta.json").read_text())) == 1


def test_duplicate_confirmation_keys_cannot_commit_a_preview(tmp_path):
    result = _run(tmp_path, _assignment(), [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        '{"customer_id":"acme","review_reason":null}',
        '{"submit_preview":false,"submit_preview":true}',
        '{"customer_id":"acme","review_reason":null}',
    ])
    assert result.output.status == "partial"
    assert json.loads((result.trace_path / "state_delta.json").read_text()) == []
