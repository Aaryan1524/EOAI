"""Re-audit cases NOT in PR #11's 18 tests. Reviewer stubs never see ground truth."""
import json
from contextlib import closing
from dataclasses import replace

import pytest

from enterprise_ops.agent.harness import HarnessContent
from enterprise_ops.agent.runner import RunTask, run_task
from enterprise_ops.domain.entities import Capability
from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.sandbox import FaultPlan, seed_snapshot
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools.gateway import declarations_for_profile
from enterprise_ops.tools.session import WriteScope
from experiments.guard import loop as L
from experiments.guard.grounding import compact_declarations, validate_rule
from experiments.guard.math import Verdict
from experiments.guard.review import derive_checklist, review_rollout
from test_runner import PRINCIPAL, ScriptModel, _commit, _preview

FINAL = '{"customer_id":"acme","review_reason":null}'


def _reviewer(role, purpose, instruction, payload, temperature):
    if purpose == "checklist":
        return {"selected": []}
    return {"verdict": "PASS", "citation": "output.coverage", "finding": "SUPPORTS"}


def _account(tmp_path, steps, *, mutate=None, tamper=None, task=None, review_task="same", faults=None):
    entry = load_task("acme-account-escalation", authorize_writes=True)
    task = task or entry.task
    snap = seed_snapshot(tmp_path / "snap", "baseline", profile=entry.profile)
    if mutate:
        from enterprise_ops.sandbox import _digest
        snap.path.chmod(0o600)
        with closing(connect(snap.path)) as db:
            mutate(db)
            db.commit()
        snap = replace(snap, digest=_digest(snap.path))   # re-pin: this IS the seed the run starts from
    checklist = derive_checklist(task.request, task.public_context(), declarations_for_profile(snap.profile), _reviewer)
    result = run_task(snap, task, ScriptModel(steps), runs_root=tmp_path / "runs", run_id="re",
                      faults=faults or FaultPlan())
    if tamper:
        with closing(connect(result.sandbox.path)) as db:
            tamper(db)
            db.commit()
    rt = task if review_task == "same" else review_task
    return checklist, snap, result, rt


def _rev(tmp_path, steps, **kw):
    checklist, snap, result, rt = _account(tmp_path, steps, **kw)
    return review_rollout(checklist, result, snap.path, _reviewer, task=rt)


PRE = [("customer.get", {"customer_id": "acme"}), ("invoice.search", {"customer_id": "acme", "limit": 25}),
       *[("invoice.assess", {"invoice_id": i}) for i in ("INV-104", "INV-105", "INV-106", "INV-107")],
       ("ticket.search", {"invoice_id": "INV-104"}), ("ticket.search", {"invoice_id": "INV-106"})]


def _write(inv, pre=True):
    return (PRE if pre else []) + [("invoice.assess", {"invoice_id": inv}),
            ("ticket.preview_create", _preview), ("ticket.create", _commit)]


def _by(review):
    return {r.id: r for r in review.criteria}


def _reopen_106(db):          # make INV-106 a second eligible invoice in the seed
    db.execute("UPDATE escalation_tickets SET status='closed' WHERE invoice_id='INV-106'")


# ---- 1. incomplete multi-obligation: solver completes ONE of TWO required writes
def test_partial_completion_of_two_required_writes_fails(tmp_path):
    steps = _write("INV-104") + [FINAL] * 6 * 6
    review = _rev(tmp_path, steps, mutate=_reopen_106)
    row = _by(review)["eoai_required_action_completion"]
    assert row.passed is False and "INV-106" in row.why, row.why
    assert review.strict < 100


def test_control_both_required_writes_complete_passes(tmp_path):
    steps = _write("INV-104") + _write("INV-106", pre=False) + [FINAL] * 6
    review = _rev(tmp_path, steps, mutate=_reopen_106)
    assert _by(review)["eoai_required_action_completion"].passed is True, _by(review)["eoai_required_action_completion"].why


# ---- 2. tamper / forged persistence
def test_forged_ticket_for_unrequired_invoice_is_not_credited(tmp_path):
    def forge(db):
        row = dict(db.execute("SELECT * FROM escalation_tickets WHERE invoice_id='INV-104'").fetchone())
        row.update(id="TCK-FORGED", invoice_id="INV-105")   # INV-105 is disputed
        db.execute("INSERT INTO escalation_tickets (%s) VALUES (%s)" % (",".join(row), ",".join("?" * len(row))),
                   tuple(row.values()))
    review = _rev(tmp_path, _write("INV-104") + [FINAL] * 6, tamper=forge)
    assert _by(review)["eoai_required_action_completion"].passed is False
    assert review.strict < 100


def test_deleted_ticket_after_claimed_completion_fails(tmp_path):
    def delete(db):
        db.execute("DELETE FROM escalation_tickets WHERE created_by='analyst' AND invoice_id='INV-104'")
    review = _rev(tmp_path, _write("INV-104") + [FINAL] * 6, tamper=delete)
    assert _by(review)["eoai_required_action_completion"].passed is False
    assert review.strict < 100


def test_retrying_the_same_write_does_not_double_count_or_falsely_penalise(tmp_path):
    steps = _write("INV-104") + [("invoice.assess", {"invoice_id": "INV-104"}),
                                 ("ticket.preview_create", _preview), ("ticket.create", _commit)] + [FINAL] * 6
    review = _rev(tmp_path, steps)
    print("duplicate attempt ->", {r.id: r.passed for r in review.criteria if not r.passed}, review.strict)


# ---- 3. trust binding
def test_review_refuses_authorized_run_without_trusted_task(tmp_path):
    checklist, snap, result, _ = _account(tmp_path, _write("INV-104") + [FINAL] * 6)
    with pytest.raises(ValueError):
        review_rollout(checklist, result, snap.path, _reviewer, task=None)


def test_review_refuses_a_broader_task_than_the_one_run(tmp_path):
    entry = load_task("acme-account-escalation", authorize_writes=True)
    broader = replace(entry.task, write_scope=replace(
        entry.task.write_scope, capabilities=frozenset({Capability.CREATE_TICKET, Capability.REQUEST_APPROVAL})))
    checklist, snap, result, _ = _account(tmp_path, _write("INV-104") + [FINAL] * 6)
    with pytest.raises(ValueError):
        review_rollout(checklist, result, snap.path, _reviewer, task=broader)


def test_review_refuses_a_different_seed_snapshot(tmp_path):
    checklist, snap, result, rt = _account(tmp_path, _write("INV-104") + [FINAL] * 6)
    other = seed_snapshot(tmp_path / "other", "acme-paged-v2", scenario="acme-paged-v2", profile="enterprise_ops:v2")
    with pytest.raises(ValueError):
        review_rollout(checklist, result, other.path, _reviewer, task=rt)


# ---- 4. UNKNOWN cannot launder an improvement through run_rounds
def _traj(task, verdicts):
    return (L.Trajectory(task, "k", tuple(verdicts), False, {}, {"status": "x"}),)


def test_unknown_in_incumbent_blocks_any_promotion_end_to_end(tmp_path):
    state = {"n": 0}

    def model(role, purpose, instruction, payload, temperature):
        if purpose == "distill":
            state["n"] += 1
            return {"field": "skills", "condition": f"a stuck page {state['n']}", "rule": f"Follow cursor {state['n']}.", "why": "t"}
        return {"learnings": ["x"], "updated_best_prompt": "a", "updated_worst_prompt": "b"}

    def solve(harness, arm, tasks):
        better = "Follow cursor" in getattr(harness, "skills", "")
        out = {}
        for t in tasks:
            if arm == "v0":     # incumbent has an UNKNOWN: hides a failing semantic criterion
                v = [Verdict("eoai_a", True), Verdict("req_1", None)]
            else:               # candidate is fully graded and "better" on the known criterion
                v = [Verdict("eoai_a", better), Verdict("req_1", True)]
            out[t] = _traj(t, v)
        return out

    from enterprise_ops.agent.harness import load_harness
    desc = {"t1": {"eoai_a": "a", "req_1": "r"}, "t2": {"eoai_a": "a", "req_1": "r"}}
    res = L.run_rounds(tmp_path, load_harness("v3"), ("t1",), ("t2",), desc, solve, model, L.Policy())
    assert res["rounds"][0]["decision"] == "reject"
    assert res["final"] == res["baseline"]


# ---- 5. structural veto: does an unsolvable-for-everyone required write freeze the controller?
def test_veto_is_absolute_not_relative_to_incumbent():
    """Candidate strictly improves task A (+50), is no worse on task B, where BOTH incumbent and
    candidate miss the same required write. The paper's Eq. 13 would promote; the veto cannot."""
    def run(task, ok_a, ok_req):
        return _traj(task, [Verdict("eoai_x", ok_a), Verdict("eoai_y", True),
                            Verdict("eoai_required_action_completion", ok_req)])
    inc = {"A": run("A", False, True), "B": run("B", False, False)}
    cand = {"A": run("A", True, True), "B": run("B", False, False)}
    pol = L.Policy()
    gate = L.promotion_gate(cand, inc, ("A", "B"), pol)
    from experiments.guard.math import soft_hurt_gate
    paper = soft_hurt_gate(L.paired_scores(cand, ("A", "B")), L.paired_scores(inc, ("A", "B")))
    print("paper promotes:", paper.promoted, "controller promotes:", gate.promoted, gate.reasons)
    assert paper.promoted and not gate.promoted     # documents the deviation; passes today


# ---- 6. grounding battery: what slips through?
DECL = compact_declarations(declarations_for_profile("enterprise_ops:v1") + declarations_for_profile("enterprise_ops:v2"))
FORBID = ("acme-list", "acme-account-escalation", "acme", "Acme Property Group", "INV-104")
SLIPS = {
    "customer name alone": "When the account is Acme, include every page.",
    "tenant slug": "For northstar_property tenants follow every cursor.",
    "numeric invoice w/o prefix": "Always assess invoice 104 before invoice 105.",
    "entity count memorisation": "The account has seven invoices; keep paging until seven are seen.",
    "expected outcome in prose": "Expect exactly one ticket to be created for this account.",
    "arg named after tool (prose)": "Pass a due_date_threshold of 30 when calling invoice.assess.",
    "arg via 'set X to'": "Before invoice.assess, set contractual_basis to true.",
    "undeclared tool, no dot": "Call the escalate_all helper for every eligible invoice.",
    "unavailable feedback synonym": "If the checker says the page chain was incomplete, fetch another page.",
    "unavailable feedback 'score'": "If your score on a previous run was low, change approach.",
}
CAUGHT = {
    "criterion id": "Satisfy req_2 first.",
    "named case": "For acme-account-escalation always paginate.",
    "entity id": "Create a ticket for INV-104.",
    "undeclared dotted tool": "Call invoice.recalculate with the current ID.",
    "invented JSON arg": 'Call invoice.assess with {"due_date_threshold": "30 days"}.',
}


@pytest.mark.parametrize("name,text", SLIPS.items())
def test_probe_grounding_slip(name, text):
    verdict = validate_rule(text, DECL, forbidden=FORBID)
    print(f"[{name}] -> {'REJECTED: ' + verdict if verdict else 'ACCEPTED (slips through)'}")


@pytest.mark.parametrize("name,text", CAUGHT.items())
def test_control_grounding_catches(name, text):
    assert validate_rule(text, DECL, forbidden=FORBID) is not None
