import json
from itertools import count

import pytest

from enterprise_ops.agent.harness import HarnessContent, load_harness
from experiments.guard.loop import Edit, Policy, Trajectory, apply_edit, exact_diff, run_rounds
from experiments.guard.math import Verdict


def test_edit_scope_exact_replacement_and_unified_diff():
    before = HarnessContent("Original", "Workflow")
    edit = Edit.parse({"field": "skills", "old": "Workflow", "new": "[iff test requested] Check.",
                       "why": "Scoped rule"})
    after = apply_edit(before, edit)
    assert after.instructions == before.instructions
    assert after.memory == before.memory
    patch = exact_diff(before, after)
    assert set(patch) == {"skills"}
    assert "-Workflow\n\\ No newline at end of file" in patch["skills"]
    with pytest.raises(ValueError, match="one exact passage"):
        apply_edit(HarnessContent("Base", "Workflow Workflow"), edit)
    with pytest.raises(ValueError, match="condition"):
        Edit.parse({"field": "skills", "old": "", "new": "Always check.", "why": "Missing guard"})


@pytest.mark.parametrize("repair_passes", [True, False])
def test_round_pairs_gate_rollbacks_and_never_promotes_failed_repair(tmp_path, repair_passes):
    serial = count()
    rollouts = []
    proposal_inputs = []

    def model(role, purpose, instruction, payload, temperature):
        if purpose == "distill":
            proposal_inputs.append(payload)
            rule = "[iff first trigger] Candidate best." if next(serial) == 0 else "[iff other trigger] Candidate worst."
            return {"field": "skills", "old": "", "new": rule, "why": "Test fixture only"}
        if purpose == "repair":
            return {"field": "skills", "old": "[iff first trigger] Candidate best.",
                    "new": "[iff narrowed trigger] Candidate repaired.", "why": "Restore replay"}
        assert purpose == "contrast"
        assert "private-development-task" not in json.dumps(payload)
        return {"learnings": ["Scope matters."], "updated_best_prompt": "Preserve scoped evidence.",
                "updated_worst_prompt": "Narrow its trigger."}

    def solve(harness, arm, tasks):
        rollouts.append((arm, tasks))
        content = getattr(harness, "skills", "")
        result = {}
        for task in tasks:
            training = task == "private-development-task"
            passed = (
                "best" in content or "repaired" in content if training
                else not content or ("repaired" in content and repair_passes)
            )
            votes = (Verdict("pinned", True), Verdict("target", passed))
            result[task] = (Trajectory(task, arm, votes, False, {}, {"status": arm}),)
        return result

    result = run_rounds(
        tmp_path, load_harness("v3"), ("private-development-task",), ("replay-task",),
        {task: {"pinned": "Always check scope", "target": "Requested result"}
         for task in ("private-development-task", "replay-task")},
        solve, model, Policy(),
    )
    decision, = result["rounds"]
    assert not decision["gate"]["promoted"]
    assert decision["gate"]["hurt"] == ("replay-task",)
    assert decision["paired_task_deltas"]["replay-task"]["continuous"] == -50
    assert decision["paired_task_deltas"]["replay-task"]["criteria"][0]["criterion_id"] == "target"
    assert decision["repair"]["gate"]["promoted"] is repair_passes
    assert decision["decision"] == ("repair" if repair_passes else "reject")
    assert (result["final"] != result["baseline"]) is repair_passes
    assert proposal_inputs[0] == proposal_inputs[1]
    assert [tasks for arm, tasks in rollouts if "-c" in arm] == [
        ("private-development-task",), ("private-development-task",),
    ]
    assert ("r1-replay", ("replay-task",)) in rollouts
    assert (tmp_path / "r1-rejection-ledger.json").exists() is not repair_passes
    for artifact in (tmp_path / "harnesses").iterdir():
        assert HarnessContent(**json.loads(artifact.read_text())).digest == artifact.stem


def test_prior_rejection_is_skipped_even_if_a_later_proposal_repeats_its_text(tmp_path):
    first = "[iff first scoped trigger] Review invoice evidence."
    alternatives = iter((first, "[iff second scoped trigger] Check the terms.", first,
                         "[iff third scoped trigger] Validate the cover.", "[iff fourth scoped trigger] Check dates."))

    def model(role, purpose, instruction, payload, temperature):
        if purpose == "distill":
            return {"field": "skills", "old": "", "new": next(alternatives), "why": "Scripted gate test"}
        return {"learnings": ["No gain."], "updated_best_prompt": "Use the evidence.",
                "updated_worst_prompt": "Narrow the trigger."}

    evaluated = []

    def solve(harness, arm, tasks):
        skills = getattr(harness, "skills", "")
        evaluated.append(skills)
        return {"seed": (Trajectory("seed", arm, (Verdict("criterion", skills in ("", first)),), False, {}, {}),)}

    result = run_rounds(tmp_path, load_harness("v3"), ("seed",), (), {"seed": {"criterion": "Visible"}},
                        solve, model, Policy(rounds=2))
    assert [round_["decision"] for round_ in result["rounds"]] == ["reject", "reject"]
    assert result["baseline"] == result["final"]
    assert evaluated.count(first) == 1
