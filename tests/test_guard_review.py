import json
from contextlib import closing

import pytest

from enterprise_ops.agent.model import ModelError
from enterprise_ops.agent.runner import RunTask, run_task
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, seed_snapshot
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools.gateway import declarations_for_profile
from experiments.guard.review import derive_checklist, review_rollout
from test_runner import PRINCIPAL, ScriptModel, _assignment, _commit, _preview


FINAL = '{"customer_id":"acme","review_reason":null}'


def _reviewer(role, purpose, instruction, payload, temperature):
    assert role == "reviewer" and temperature == 0.0
    if purpose == "checklist":
        assert set(payload) == {
            "request", "public_context", "tool_declarations", "deterministic_criteria", "semantic_options",
        }
        assert "expectations" not in json.dumps(payload)
        return {"selected": ["s1"]}
    assert purpose == "criterion"
    assert set(payload) == {"request", "public_context", "tool_names", "criterion", "evidence"}
    assert "rendered_instructions" not in json.dumps(payload)
    return {"passed": True, "why": "The final coverage identifies the account.", "evidence": ["output.coverage"]}


def _prepare(tmp_path, task, scenario="baseline"):
    snapshot = seed_snapshot(tmp_path / "snapshots", scenario, scenario=scenario,
                             profile="enterprise_ops:v2" if scenario.endswith("v2") else "enterprise_ops:v1")
    checklist = derive_checklist(
        task.request, task.public_context(), declarations_for_profile(snapshot.profile), _reviewer,
    )
    return checklist, snapshot


def _run(tmp_path, snapshot, task, steps, **kwargs):
    return run_task(
        snapshot, task, ScriptModel(steps), runs_root=tmp_path / "runs", run_id="practice", **kwargs,
    )


def _verdicts(review):
    return {row.id: row.passed for row in review.criteria}


def test_guard_review_checks_v2_pagination_and_never_reads_expected_answers(tmp_path):
    task = RunTask("inventory", "List every Acme invoice.", PRINCIPAL, "list_invoices", customer_id="acme")
    checklist, snapshot = _prepare(tmp_path, task, "acme-paged-v2")

    def page(data):
        return {"account_ref": "acme", "page_size": 2, "page_cursor": data["next_cursor"]}

    result = _run(tmp_path, snapshot, task, [
        ("invoice.search", {"account_ref": "acme", "page_size": 2}),
        ("invoice.search", page), ("invoice.search", page), ("invoice.search", page), FINAL,
    ])
    review = review_rollout(checklist, result, snapshot.path, _reviewer)
    assert result.output.coverage.invoice_search_exhausted
    assert all(_verdicts(review).values()), review
    assert review.strict == review.continuous == 100

    trace_path = result.trace_path / "trace.jsonl"
    rows = [json.loads(line) for line in trace_path.read_text().splitlines()]
    calls = [row for row in rows if row["event"] == "tool"]
    calls[1]["arguments"].pop("page_cursor")
    trace_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    review = review_rollout(checklist, result, snapshot.path, _reviewer)
    assert not _verdicts(review)["eoai_invoice_coverage"]
    assert review.strict == review.continuous / 2


def test_guard_checks_actual_database_and_exact_commit_receipt_readback(tmp_path):
    task = _assignment()
    checklist, snapshot = _prepare(tmp_path, task)
    result = _run(tmp_path, snapshot, task, [
        ("invoice.get", {"invoice_id": "INV-104"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview), ("ticket.create", _commit), FINAL,
    ], faults=FaultPlan((FaultStep("ticket.create", 1, FaultKind.LOSE_RESPONSE),)))
    review = review_rollout(checklist, result, snapshot.path, _reviewer)
    assert all(_verdicts(review).values()), review
    assert review.strict == 100

    with closing(connect(result.sandbox.path)) as db:
        db.execute("UPDATE escalation_tickets SET reason_code = 'OTHER_REASON'")
    review = review_rollout(checklist, result, snapshot.path, _reviewer)
    assert not _verdicts(review)["eoai_scoped_audit"]


def test_guard_read_only_compares_sqlite_instead_of_trusting_empty_audit(tmp_path):
    task = RunTask("inventory", "List every Acme invoice.", PRINCIPAL, "list_invoices", customer_id="acme")
    checklist, snapshot = _prepare(tmp_path, task)
    result = _run(tmp_path, snapshot, task, [("invoice.search", {"customer_id": "acme"}), FINAL])
    with closing(connect(result.sandbox.path)) as db:
        db.execute("UPDATE invoices SET total_minor = total_minor + 1 WHERE id = 'INV-104'")
    review = review_rollout(checklist, result, snapshot.path, _reviewer)
    assert _verdicts(review)["eoai_invoice_coverage"]
    assert not _verdicts(review)["eoai_read_only"]


@pytest.mark.parametrize("verdict", [
    {"passed": "true", "why": "Semantic answer.", "evidence": ["output.coverage"]},
    {"passed": True, "why": "Semantic answer.", "evidence": ["output.missing.nonexistent"]},
    {"passed": True, "why": "Semantic answer.", "evidence": []},
    {"passed": True, "why": "Semantic answer.", "evidence": ["output.coverage"], "extra": 0},
])
def test_invalid_semantic_verdict_fails_closed(tmp_path, verdict):
    task = RunTask("inventory", "List every Acme invoice.", PRINCIPAL, "list_invoices", customer_id="acme")
    checklist, snapshot = _prepare(tmp_path, task)
    result = _run(tmp_path, snapshot, task, [("invoice.search", {"customer_id": "acme"}), FINAL])
    review = review_rollout(checklist, result, snapshot.path, lambda *args: verdict)
    assert not _verdicts(review)["req_1"]
    assert review.strict == review.continuous / 2


def test_blocking_runtime_error_applies_zero_strict_score(tmp_path):
    task = RunTask("inventory", "List every Acme invoice.", PRINCIPAL, "list_invoices", customer_id="acme")
    checklist, snapshot = _prepare(tmp_path, task)
    result = _run(tmp_path, snapshot, task, [
        ("invoice.search", {"customer_id": "acme"}), ModelError("provider unavailable"),
    ])
    review = review_rollout(checklist, result, snapshot.path, _reviewer)
    assert review.blocking_error and review.continuous > 0 and review.strict == 0
    assert not _verdicts(review)["eoai_clean_execution"]


def test_checklist_rejects_executable_hooks_and_wrong_ids():
    task = _assignment()
    with pytest.raises(ValueError, match="Invalid semantic checklist criterion"):
        derive_checklist(
            task.request, task.public_context(), declarations_for_profile("enterprise_ops:v1"),
            lambda *args: {"selected": ["s999"]},
        )


def test_checklist_retries_without_execution_and_rejects_unsourced_criteria():
    task = RunTask("inventory", "List all invoices for Acme.", PRINCIPAL, "list_invoices", customer_id="acme")
    replies = iter((
        {"selected": ["Use contract.get"]},
        {"selected": ["s1"]},
    ))
    prompts = []

    def reply(role, purpose, instruction, payload, temperature):
        prompts.append(payload)
        return next(replies)

    checklist = derive_checklist(
        task.request, task.public_context(), declarations_for_profile("enterprise_ops:v1"), reply,
    )
    assert checklist.criteria[-1].description == "List all invoices for Acme"
    assert len(prompts) == 2 and prompts[0] == prompts[1]


def test_reviewer_infrastructure_error_invalidates_pair(tmp_path):
    task = RunTask("inventory", "List every Acme invoice.", PRINCIPAL, "list_invoices", customer_id="acme")
    checklist, snapshot = _prepare(tmp_path, task)
    result = _run(tmp_path, snapshot, task, [("invoice.search", {"customer_id": "acme"}), FINAL])

    def timeout(*args):
        raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        review_rollout(checklist, result, snapshot.path, timeout)
