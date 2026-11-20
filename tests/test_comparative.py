import json
from itertools import count

from enterprise_ops.agent.harness import load_harness
from experiments.comparative import naive as naive_module
from experiments.comparative.runner import no_veto_gate
from experiments.guard import loop
from experiments.guard.loop import Policy, Trajectory, run_rounds
from experiments.guard.math import Verdict

DESCRIPTIONS = {"train": {"target": "Requested result", "eoai_required_action_completion": "Writes done"},
                "replay": {"target": "Requested result", "eoai_required_action_completion": "Writes done"}}


def _model(serial=None):
    serial = serial or count()

    def model(role, purpose, instruction, payload, temperature):
        if purpose == "distill":
            n = next(serial)
            return {"field": "skills", "condition": f"trigger number {n}", "rule": f"Rule variant {n}.",
                    "why": "Test fixture"}
        if purpose == "repair":
            return {"condition": "narrowed trigger", "rule": "Repaired rule.", "why": "Restore"}
        return {"learnings": ["x"], "updated_best_prompt": "Keep scope.", "updated_worst_prompt": "Narrow."}
    return model


def _traj(task, arm, target, write=True):
    return Trajectory(task, arm, (Verdict("target", target), Verdict("eoai_required_action_completion", write)),
                      False, {}, {"status": arm})


def test_naive_always_promotes_best_mean_even_when_worse(tmp_path):
    def solve(harness, arm, tasks):
        skills = getattr(harness, "skills", "")
        # v0 passes; every candidate fails: a naive arm must still promote.
        return {t: (_traj(t, arm, "Rule variant" not in skills),) for t in tasks}

    result = naive_module.run_naive(tmp_path, load_harness("v3"), ("train",), {"train": DESCRIPTIONS["train"]},
                                    solve, _model(), Policy(rounds=2), declarations=loop._DEFAULT_DECLARATIONS)
    assert [r["decision"] for r in result["rounds"]] == ["accept", "accept"]
    assert result["final"] != result["baseline"]
    assert not (tmp_path / "r1-regret.json").exists()


def test_naive_reflection_is_flat_failure_text_without_regret_fields():
    incumbent = {"a": (_traj("a", "v0", False),), "b": (_traj("b", "v0", True),)}
    evidence = naive_module.reflection(incumbent, ("a", "b"), {"a": DESCRIPTIONS["train"], "b": DESCRIPTIONS["train"]})
    assert [row["task"] for row in evidence["failed_tasks"]] == ["a"]
    text = json.dumps(evidence)
    for field in ("priority", "hurt", "gap", "mastered", "contrast", "rejections"):
        assert field not in text


def test_no_veto_ablation_promotes_where_veto_blocks(tmp_path):
    # Candidate helps the training task; both incumbent and candidate miss the same write on replay.
    def solve(harness, arm, tasks):
        improved = "Rule variant" in getattr(harness, "skills", "")
        return {t: (_traj(t, arm, improved if t == "train" else False, write=(t == "train")),) for t in tasks}

    (tmp_path / "veto").mkdir()
    (tmp_path / "noveto").mkdir()
    veto = run_rounds(tmp_path / "veto", load_harness("v3"), ("train",), ("replay",), DESCRIPTIONS, solve,
                      _model(), Policy())
    assert veto["final"] == veto["baseline"]
    assert "required_action_incomplete" in veto["rounds"][0]["gate"]["reasons"]
    original = loop.promotion_gate
    try:
        loop.promotion_gate = no_veto_gate
        ablation = run_rounds(tmp_path / "noveto", load_harness("v3"), ("train",), ("replay",), DESCRIPTIONS,
                              solve, _model(), Policy())
    finally:
        loop.promotion_gate = original
    assert ablation["final"] != ablation["baseline"]
    assert loop.promotion_gate is original


def test_no_veto_still_requires_reliable_reviews():
    unknown = {"t": (Trajectory("t", "c", (Verdict("target", None),), False, {}, {}),)}
    known = {"t": (_traj("t", "v0", False),)}
    decision = no_veto_gate(unknown, known, ("t",), Policy())
    assert not decision.promoted and decision.reasons == ("unreliable_review",)
