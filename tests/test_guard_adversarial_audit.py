"""Independent adversarial audit cases, integrated after remediation."""
import json
from contextlib import closing
from dataclasses import replace

import pytest

from enterprise_ops.agent.runner import RunLimits, RunTask, run_task
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, seed_snapshot
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools.gateway import declarations_for_profile
from experiments.guard import loop as guard_loop
from experiments.guard.review import derive_checklist, review_rollout
from test_runner import PRINCIPAL, ScriptModel, _commit, _preview
from enterprise_ops.domain.entities import Capability
from enterprise_ops.tools.session import WriteScope

FINAL = '{"customer_id":"acme","review_reason":null}'
REQUEST = ("Review Acme Property Group invoice INV-104 under current policy. "
           "If eligible, create and verify an escalation ticket when authorized.")


def _task(invoice="INV-104"):
    scope = WriteScope(PRINCIPAL, "audit", "acme", frozenset({Capability.CREATE_TICKET}),
                       invoice_ids=frozenset({invoice}))
    return RunTask("acme-escalation", REQUEST.replace("INV-104", invoice), PRINCIPAL, "escalate", customer_id="acme",
                   invoice_ids=frozenset({invoice}), write_scope=scope)


def _all_pass(selected):
    def reviewer(role, purpose, instruction, payload, temperature):
        if purpose == "checklist":
            return {"selected": selected}
        return {"verdict": "PASS", "why": "The final coverage identifies the account.",
                "evidence": [{"key": "output.coverage", "quote": "acme", "finding": "SUPPORTS"}]}
    return reviewer


def _rollout(tmp_path, steps, selected=("s1",), faults=None, task=None, limits=None):
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline", profile="enterprise_ops:v1")
    task = task or _task()
    checklist = derive_checklist(task.request, task.public_context(),
                                 declarations_for_profile(snapshot.profile), _all_pass(list(selected)))
    result = run_task(snapshot, task, ScriptModel(steps), runs_root=tmp_path / "runs", run_id="audit",
                      faults=faults or FaultPlan(), limits=limits)
    return checklist, snapshot, result, review_rollout(checklist, result, snapshot.path, _all_pass(list(selected)), task=task)


READ_ONLY = [("invoice.get", {"invoice_id": "INV-104"}), ("invoice.assess", {"invoice_id": "INV-104"}), FINAL, FINAL, FINAL, FINAL]
WITH_WRITE = [("invoice.get", {"invoice_id": "INV-104"}), ("invoice.assess", {"invoice_id": "INV-104"}),
              ("ticket.preview_create", _preview), ("ticket.create", _commit), FINAL]


def test_control_correct_write_scores_100(tmp_path):
    *_, review = _rollout(tmp_path, WITH_WRITE)
    assert review.strict == review.continuous == 100


def test_assessed_eligible_authorized_but_unwritten_is_not_perfect(tmp_path):
    _, _, result, review = _rollout(tmp_path, READ_ONLY)
    assert result.output.decisions[0].decision == "eligible"            # solver itself found it eligible
    assert not any(a.status == "completed" for a in result.output.actions)  # and wrote nothing
    assert review.strict < 100


def test_omitted_write_scores_strictly_below_correct_write(tmp_path):
    omitted = _rollout(tmp_path / "a", READ_ONLY)[-1]
    done = _rollout(tmp_path / "b", WITH_WRITE)[-1]
    assert omitted.continuous < done.continuous and omitted.strict < done.strict


def test_write_obligation_exists_even_when_selector_returns_nothing(tmp_path):
    _, _, _, review = _rollout(tmp_path, READ_ONLY, selected=())
    ids = {row.id for row in review.criteria}
    assert any("write" in i or "action" in i or "complet" in i for i in ids), ids
    assert review.strict < 100


def test_solver_self_reported_not_attempted_is_penalized(tmp_path):
    """Documents the mechanism: the solver's own output says ACTION_NOT_ATTEMPTED."""
    _, _, result, review = _rollout(tmp_path, READ_ONLY)
    codes = {m.code for m in result.output.missing_information}
    # Preserve the historical incomplete output and assert the repaired reviewer catches it.
    assert "ACTION_NOT_ATTEMPTED" in codes or any(a.status == "not_attempted" for a in result.output.actions)
    assert review.strict < 100


def test_negative_result_pagination_incompleteness_is_caught(tmp_path):
    """Pagination is NOT a false-pass source: stopping after page 1 fails invoice_coverage."""
    snapshot = seed_snapshot(tmp_path / "snapshots", "acme-paged-v2", scenario="acme-paged-v2",
                             profile="enterprise_ops:v2")
    task = RunTask("inv", "List every Acme invoice.", PRINCIPAL, "list_invoices", customer_id="acme")
    checklist = derive_checklist(task.request, task.public_context(),
                                 declarations_for_profile(snapshot.profile), _all_pass(["s1"]))
    result = run_task(snapshot, task, ScriptModel([
        ("invoice.search", {"account_ref": "acme", "page_size": 2}), FINAL, FINAL, FINAL]),
        runs_root=tmp_path / "runs", run_id="audit")
    review = review_rollout(checklist, result, snapshot.path, _all_pass(["s1"]))
    verdicts = {r.id: r.passed for r in review.criteria}
    assert not verdicts["eoai_invoice_coverage"] and review.strict < 100


def test_deterministic_rationale_distinguishes_pass_from_failed_verification(tmp_path):
    """A tampered persisted record must produce concrete failure evidence."""
    ok = _rollout(tmp_path / "a", WITH_WRITE)[-1]
    bad_paths = tmp_path / "b"
    cl, snap, res, _ = _rollout(bad_paths, WITH_WRITE)
    with closing(connect(res.sandbox.path)) as db:
        db.execute("UPDATE escalation_tickets SET reason_code = 'OTHER'")
    bad = review_rollout(cl, res, snap.path, _all_pass(["s1"]), task=_task())
    p = {r.id: r.why for r in ok.criteria}
    f = {r.id: r.why for r in bad.criteria if not r.passed}
    assert f and f["eoai_scoped_audit"] != p["eoai_scoped_audit"]


def test_reviewer_invalid_verdict_is_not_routed_as_solver_failure(tmp_path):
    from experiments.guard.math import Verdict
    from experiments.guard.loop import Trajectory, _diagnostics
    bad = Verdict("req_1", False, "Reviewer failed to return a valid evidence-backed verdict.")
    batch = {"t": (Trajectory("t", "k", (bad,), False, {}, {}),)}
    out = _diagnostics(batch, None, None, [batch], ("t",), {"t": {"req_1": "x"}}, [])
    shown = json.dumps(out)
    assert "Reviewer failed" not in shown


def test_candidate_rule_may_not_embed_criterion_or_task_ids():
    try:
        guard_loop.Edit.from_proposal({"field": "instructions", "condition": "overdue invoice review",
                                       "rule": "fulfil criterion_id: req_2 for acme-account-escalation", "why": "x"})
    except ValueError:
        return
    raise AssertionError("candidate rule was accepted")


def test_candidate_rule_may_not_invent_tool_arguments():
    try:
        guard_loop.Edit.from_proposal({"field": "instructions", "condition": "assessing an overdue invoice",
                                       "rule": 'call invoice.assess with {"due_date_threshold": "30 days"}', "why": "x"})
    except ValueError:
        return
    raise AssertionError("candidate rule was accepted")


def _completion(review):
    return next(row for row in review.criteria if row.id == "eoai_required_action_completion")


def test_obligation_detected_even_without_a_solver_assessment(tmp_path):
    *_, review = _rollout(tmp_path, [READ_ONLY[0], FINAL, FINAL], selected=())
    assert _completion(review).passed is False
    assert "Missing required persisted action" in _completion(review).why


def test_failed_write_does_not_discharge_obligation(tmp_path):
    fault = FaultPlan((FaultStep("ticket.create", 1, FaultKind.TRANSIENT_FAILURE),))
    *_, result, review = _rollout(tmp_path, WITH_WRITE, selected=(), faults=fault,
                                  limits=RunLimits(tool_retries=0))
    assert json.loads((result.trace_path / "state_delta.json").read_text()) == []
    events = [json.loads(line) for line in (result.trace_path / "trace.jsonl").read_text().splitlines()]
    assert any(row.get("tool") == "ticket.create" and row.get("event") == "tool" for row in events)
    assert _completion(review).passed is False
    assert "Missing required persisted action" in _completion(review).why


@pytest.mark.parametrize("invoice", ["INV-105", "INV-106"])
def test_disputed_invoice_or_existing_ticket_requires_no_new_write(tmp_path, invoice):
    steps = [(tool, {"invoice_id": invoice}) for tool in ("invoice.get", "invoice.assess")] + [FINAL, FINAL]
    *_, review = _rollout(tmp_path, steps, selected=(), task=_task(invoice))
    assert _completion(review).passed is True
    assert "No new authorized action" in _completion(review).why


def test_unauthorized_attempt_is_distinguished_from_no_action_required(tmp_path):
    task = replace(_task(), write_scope=None)
    attempt = ("ticket.create", {
        "preview_token": "fake-preview", "idempotency_key": "fake-key",
        "expected_tenant_revision": 0, "expected_invoice_version": 1, "expected_policy_version": 1,
    })
    *_, read = _rollout(tmp_path / "read", READ_ONLY, task=task, selected=())
    *_, unauthorized = _rollout(tmp_path / "denied", [*READ_ONLY[:2], attempt, FINAL], task=task, selected=())
    assert _completion(read).passed is True
    assert _completion(unauthorized).passed is False
    assert "Unauthorized" in _completion(unauthorized).why


def test_grounding_rejects_unavailable_feedback_and_actual_active_task_id():
    for condition in ("the reviewer failed to return a verdict", "account code comparison-task"):
        with pytest.raises(ValueError):
            guard_loop.Edit.from_proposal({
                "field": "instructions", "condition": condition, "rule": "Follow the cursor.", "why": "test",
            }, forbidden=("comparison-task",))


def test_missing_required_action_vetoes_positive_numeric_gate(tmp_path):
    from experiments.guard.math import Verdict, soft_hurt_gate

    _, _, _, review = _rollout(tmp_path, READ_ONLY, selected=())
    replay = tuple(Verdict(row.id, row.passed, row.why) for row in review.criteria)

    def run(task, verdicts):
        return (guard_loop.Trajectory(task, task, verdicts, False, {}, {}),)

    tasks = ("other", "replay")
    incumbent = {"other": run("other", (Verdict("one", False),)), "replay": run("replay", replay)}
    candidate = {"other": run("other", (Verdict("one", True),)), "replay": run("replay", replay)}
    assert soft_hurt_gate(*(guard_loop.paired_scores(batch, tasks) for batch in (candidate, incumbent))).promoted
    gate = guard_loop.promotion_gate(candidate, incumbent, tasks, guard_loop.Policy())
    assert not gate.promoted and "required_action_incomplete" in gate.reasons


def test_unknown_verdict_disallows_promotion_despite_better_known_score():
    from experiments.guard.math import Verdict

    task = ("case",)
    before = {"case": (guard_loop.Trajectory("case", "old", (Verdict("one", False), Verdict("two", False)),
                                             False, {}, {}),)}
    after = {"case": (guard_loop.Trajectory("case", "new", (Verdict("one", True), Verdict("two", None)),
                                            False, {}, {}),)}
    assert not guard_loop.promotion_gate(after, before, task, guard_loop.Policy()).promoted
    assert guard_loop.promotion_gate(after, before, task, guard_loop.Policy()).reasons == ("unreliable_review",)
