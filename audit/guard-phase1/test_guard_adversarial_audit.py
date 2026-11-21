"""Independent adversarial audit of GUARD Phase 1 (PR #10).

Each xfail(strict=True) test states the CORRECT behaviour. It fails today (documents the defect) and
must start passing when the defect is fixed (strict=True turns an unexpected pass into an error, forcing
the marker to be removed). No test reads held-out data or expectations; the semantic reviewer is a
stub that answers "pass" to everything, which isolates the deterministic layer.
"""
import json
from contextlib import closing

import pytest

from enterprise_ops.agent.runner import RunTask
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, seed_snapshot
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools.gateway import declarations_for_profile
from experiments.guard import loop as guard_loop
from experiments.guard.review import derive_checklist, review_rollout
from test_runner import PRINCIPAL, ScriptModel, _commit, _preview
from enterprise_ops.agent.runner import run_task
from enterprise_ops.domain.entities import Capability
from enterprise_ops.tools.session import WriteScope

FINAL = '{"customer_id":"acme","review_reason":null}'
REQUEST = ("Review Acme Property Group invoice INV-104 under current policy. "
           "If eligible, create and verify an escalation ticket when authorized.")


def _task():
    scope = WriteScope(PRINCIPAL, "audit", "acme", frozenset({Capability.CREATE_TICKET}),
                       invoice_ids=frozenset({"INV-104"}))
    return RunTask("acme-escalation", REQUEST, PRINCIPAL, "escalate", customer_id="acme",
                   invoice_ids=frozenset({"INV-104"}), write_scope=scope)


def _all_pass(selected):
    def reviewer(role, purpose, instruction, payload, temperature):
        if purpose == "checklist":
            return {"selected": selected}
        return {"passed": True, "why": "ok", "evidence": ["output.coverage"]}
    return reviewer


def _rollout(tmp_path, steps, selected=("s1",), faults=None):
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline", profile="enterprise_ops:v1")
    task = _task()
    checklist = derive_checklist(task.request, task.public_context(),
                                 declarations_for_profile(snapshot.profile), _all_pass(list(selected)))
    result = run_task(snapshot, task, ScriptModel(steps), runs_root=tmp_path / "runs", run_id="audit",
                      faults=faults or FaultPlan())
    return checklist, snapshot, result, review_rollout(checklist, result, snapshot.path, _all_pass(list(selected)))


READ_ONLY = [("invoice.get", {"invoice_id": "INV-104"}), ("invoice.assess", {"invoice_id": "INV-104"}), FINAL, FINAL, FINAL, FINAL]
WITH_WRITE = [("invoice.get", {"invoice_id": "INV-104"}), ("invoice.assess", {"invoice_id": "INV-104"}),
              ("ticket.preview_create", _preview), ("ticket.create", _commit), FINAL]


def test_control_correct_write_scores_100(tmp_path):
    *_, review = _rollout(tmp_path, WITH_WRITE)
    assert review.strict == review.continuous == 100


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="CRITICAL F1: eligible+authorized but unwritten scores 100/100")
def test_assessed_eligible_authorized_but_unwritten_is_not_perfect(tmp_path):
    _, _, result, review = _rollout(tmp_path, READ_ONLY)
    assert result.output.decisions[0].decision == "eligible"            # solver itself found it eligible
    assert not any(a.status == "completed" for a in result.output.actions)  # and wrote nothing
    assert review.strict < 100


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="CRITICAL F1: omission is weakly dominant over correct execution")
def test_omitted_write_scores_strictly_below_correct_write(tmp_path):
    omitted = _rollout(tmp_path / "a", READ_ONLY)[-1]
    done = _rollout(tmp_path / "b", WITH_WRITE)[-1]
    assert omitted.continuous < done.continuous and omitted.strict < done.strict


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="CRITICAL F1: no write obligation survives an empty semantic selection")
def test_write_obligation_exists_even_when_selector_returns_nothing(tmp_path):
    _, _, _, review = _rollout(tmp_path, READ_ONLY, selected=())
    ids = {row.id for row in review.criteria}
    assert any("write" in i or "action" in i or "complet" in i for i in ids), ids
    assert review.strict < 100


def test_solver_self_reported_not_attempted_still_scores_100(tmp_path):
    """Documents the mechanism: the solver's own output says ACTION_NOT_ATTEMPTED."""
    _, _, result, review = _rollout(tmp_path, READ_ONLY)
    codes = {m.code for m in result.output.missing_information}
    # Positive evidence of the defect (not xfail): output admits the gap, review is still perfect.
    assert "ACTION_NOT_ATTEMPTED" in codes or any(a.status == "not_attempted" for a in result.output.actions)
    assert review.strict == 100


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


def test_deterministic_rationale_is_identical_for_pass_and_fail(tmp_path):
    """MAJOR F4: failure evidence given to the proposer is a static string, never trace-derived."""
    ok = _rollout(tmp_path / "a", WITH_WRITE)[-1]
    with closing(connect(_rollout(tmp_path / "x", WITH_WRITE)[2].sandbox.path)):
        pass
    bad_paths = tmp_path / "b"
    cl, snap, res, _ = _rollout(bad_paths, WITH_WRITE)
    with closing(connect(res.sandbox.path)) as db:
        db.execute("UPDATE escalation_tickets SET reason_code = 'OTHER'")
    bad = review_rollout(cl, res, snap.path, _all_pass(["s1"]))
    p = {r.id: r.why for r in ok.criteria}
    f = {r.id: r.why for r in bad.criteria if not r.passed}
    assert f and all(f[k] == p[k] for k in f)   # same text whether pass or fail => uninformative


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="MAJOR F5: reviewer-format failures are fed to the proposer as solver failures")
def test_reviewer_invalid_verdict_is_not_routed_as_solver_failure(tmp_path):
    from experiments.guard.math import Verdict
    from experiments.guard.loop import Trajectory, _diagnostics
    bad = Verdict("req_1", False, "Reviewer failed to return a valid evidence-backed verdict.")
    batch = {"t": (Trajectory("t", "k", (bad,), False, {}, {}),)}
    out = _diagnostics(batch, None, None, [batch], ("t",), {"t": {"req_1": "x"}}, [])
    shown = json.dumps(out)
    assert "Reviewer failed" not in shown


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="MAJOR F6: task/criterion identifiers are not rejected in candidate rules")
def test_candidate_rule_may_not_embed_criterion_or_task_ids():
    try:
        guard_loop.Edit.from_proposal({"field": "instructions", "condition": "overdue invoice review",
                                       "rule": "fulfil criterion_id: req_2 for acme-account-escalation", "why": "x"})
    except ValueError:
        return
    raise AssertionError("candidate rule was accepted")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="MAJOR F6: rule text may reference undeclared tool arguments")
def test_candidate_rule_may_not_invent_tool_arguments():
    try:
        guard_loop.Edit.from_proposal({"field": "instructions", "condition": "assessing an overdue invoice",
                                       "rule": 'call invoice.assess with {"due_date_threshold": "30 days"}', "why": "x"})
    except ValueError:
        return
    raise AssertionError("candidate rule was accepted")
