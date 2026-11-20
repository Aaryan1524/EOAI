import pytest

from experiments.guard.math import (
    CriterionStatus,
    GateReason,
    TrajectoryContrast,
    TrajectoryScore,
    Verdict,
    average_scores,
    candidate_fitness,
    rank_criteria,
    score_trajectory,
    soft_hurt_gate,
    trajectory_contrast,
)


def _rollout(**grades):
    return tuple(Verdict(criterion_id, passed) for criterion_id, passed in grades.items())


def _batch(**scores):
    return {task_id: TrajectoryScore(score, 0) for task_id, score in scores.items()}


def test_trajectory_scores_use_only_graded_verdicts_and_cap_strict():
    all_pass = _rollout(required=True, unconfigured=None)
    partial = _rollout(required=True, unmet=False, unconfigured=None)

    assert score_trajectory(all_pass) == TrajectoryScore(100, 100)
    assert score_trajectory(all_pass, blocking_error=True) == TrajectoryScore(100, 0)
    assert score_trajectory(partial) == TrajectoryScore(50, 25)
    assert score_trajectory(partial, blocking_error=True) == TrajectoryScore(50, 0)
    assert score_trajectory(_rollout(required=False)) == TrajectoryScore(0, 0)
    assert average_scores([score_trajectory(all_pass), score_trajectory(partial)]) == (
        TrajectoryScore(75, 62.5)
    )

    with pytest.raises(ValueError, match="must be graded"):
        score_trajectory(_rollout(unconfigured=None))
    with pytest.raises(ValueError, match="duplicate criterion"):
        score_trajectory((*all_pass, Verdict("required", False)))
    with pytest.raises(ValueError, match="must be binary"):
        Verdict("required", 1)


def test_binary_regret_partitions_and_skips_missing_incumbent_grade():
    incumbent = [_rollout(regressed=False, gap=False, mastered=True, new=False, missing=None)]
    previous = [_rollout(regressed=True, gap=False, mastered=False, missing=True, old=True)]

    priorities = rank_criteria(incumbent, previous)
    assert [(item.criterion_id, item.hurt, item.gap, item.priority, item.status)
            for item in priorities] == [
        ("regressed", 1, 1, 2, CriterionStatus.REGRESSED),
        ("gap", 0, 1, 1, CriterionStatus.UNSOLVED_GAP),
        ("new", 0, 1, 1, CriterionStatus.UNSOLVED_GAP),
        ("mastered", 0, 0, 0, CriterionStatus.MASTERED),
    ]
    assert priorities[2].previous_obedience is None
    assert all(not item.hurt for item in rank_criteria(incumbent))


def test_fractional_obedience_ignores_ungraded_and_breaks_priority_ties_with_hurt():
    incumbent = [
        _rollout(small_drop=True, big_drop=True, gap=False),
        _rollout(small_drop=False, big_drop=False, gap=False),
        _rollout(small_drop=False, big_drop=None, gap=False),
        _rollout(small_drop=False, gap=False),
    ]
    previous = [
        _rollout(small_drop=True, big_drop=True),
        _rollout(small_drop=False, big_drop=None),
    ]

    priorities = rank_criteria(incumbent, previous)
    assert [(item.criterion_id, item.incumbent_obedience, item.hurt, item.priority)
            for item in priorities] == [
        ("big_drop", 0.5, 0.5, 1),
        ("small_drop", 0.25, 0.25, 1),
        ("gap", 0, 0, 1),
    ]


def test_rejected_winner_changes_hurt_and_preserves_incumbent_gap():
    incumbent = [_rollout(regressed=True, gap=False, missing=True, mastered=True)]
    prior = [_rollout(regressed=False, gap=True, missing=True, mastered=True)]
    rejected = [_rollout(regressed=False, gap=True, missing=None, mastered=True)]

    priorities = {item.criterion_id: item for item in rank_criteria(
        incumbent, prior, regression_candidate=rejected,
    )}
    assert priorities["regressed"].previous_obedience == 0
    assert priorities["regressed"].regression_candidate_obedience == 0
    assert priorities["regressed"].hurt == 1
    assert priorities["regressed"].gap == 0
    assert priorities["gap"].hurt == 0
    assert priorities["gap"].gap == 1
    assert priorities["missing"].regression_candidate_obedience is None
    assert priorities["missing"].status == CriterionStatus.MASTERED
    assert priorities["mastered"].status == CriterionStatus.MASTERED


def test_contrast_uses_strict_then_continuous_with_inclusive_continuous_threshold():
    assert trajectory_contrast([TrajectoryScore(60, 0), TrajectoryScore(50, 0)]) == (
        TrajectoryContrast(0, 1, 0, 10)
    )
    assert trajectory_contrast([TrajectoryScore(59, 0), TrajectoryScore(50, 0)]) is None
    assert trajectory_contrast([TrajectoryScore(30, 15), TrajectoryScore(50, 0)]) == (
        TrajectoryContrast(0, 1, 15, -20)
    )
    assert trajectory_contrast([TrajectoryScore(50, 0)] * 2, threshold=0) is None
    assert trajectory_contrast([TrajectoryScore(50, 0)]) is None


def test_candidate_fitness_uses_common_tasks_and_population_standard_deviation():
    incumbent = _batch(a=50, b=50, c=50)
    candidates = [
        {"a": TrajectoryScore(70, 35), "b": TrajectoryScore(30, 15), "c": TrajectoryScore(65, 32.5)},
        {"a": TrajectoryScore(65, 32.5), "b": TrajectoryScore(35, 17.5), "c": TrajectoryScore(65, 32.5)},
    ]

    pool = candidate_fitness(candidates, incumbent)
    first, second = pool.fitness
    assert first.mean_score == second.mean_score == TrajectoryScore(55, 27.5)
    assert first.performance == second.performance == 41.25
    assert first.helped == ("a",)
    assert first.hurt == ("b",)
    assert second.helped == second.hurt == ()
    assert first.reward == 39.25
    assert second.reward == 41.25
    assert pool.mean_reward == 40.25
    assert pool.reward_scale == 1 + 1e-6
    assert first.standardized == -1 / pool.reward_scale
    assert second.standardized == 1 / pool.reward_scale
    assert (pool.best_index, pool.worst_index) == (1, 0)


def test_candidate_fitness_ties_order_by_continuous_hurts_then_index():
    incumbent = _batch(a=50, b=50)
    regressing = {"a": TrajectoryScore(70, 40), "b": TrajectoryScore(30, 40)}
    higher_continuous = {task_id: TrajectoryScore(54, 37) for task_id in incumbent}
    safe = {task_id: TrajectoryScore(50, 38) for task_id in incumbent}

    pool = candidate_fitness([regressing, safe, safe, higher_continuous], incumbent)
    assert [fitness.reward for fitness in pool.fitness] == [50.5] * 4
    assert all(fitness.standardized == 0 for fitness in pool.fitness)
    assert pool.reward_scale == 1e-6
    assert (pool.best_index, pool.worst_index) == (3, 0)

    pool = candidate_fitness([regressing, safe, safe], incumbent)
    assert (pool.best_index, pool.worst_index) == (1, 0)
    pool = candidate_fitness([safe, safe], incumbent)
    assert (pool.best_index, pool.worst_index) == (0, 1)

    with pytest.raises(ValueError, match="same nonempty"):
        candidate_fitness([safe, _batch(a=50)], incumbent)


def test_soft_hurt_uses_strict_gain_and_counts_full_drop_over_all_tasks():
    incumbent = _batch(a=55, b=55)
    tied = soft_hurt_gate(_batch(a=95, b=35), incumbent)
    assert tied.helped == ("a",)
    assert tied.hurt == ("b",)
    assert tied.net_gain == tied.hurt_penalty == tied.required_gain == 10
    assert not tied.promoted
    assert tied.reasons == (GateReason.INSUFFICIENT_GAIN,)
    assert tied.repair_eligible

    improved = soft_hurt_gate(_batch(a=96, b=35), incumbent)
    assert improved.net_gain == 10.5
    assert improved.promoted
    assert not improved.repair_eligible


def test_soft_hurt_ignores_noise_floor_equality_and_uses_continuous_scores():
    incumbent = {"a": TrajectoryScore(60, 30), "b": TrajectoryScore(50, 25)}
    unchanged = soft_hurt_gate(incumbent, incumbent)
    assert unchanged.net_gain == unchanged.required_gain == 0
    assert not unchanged.promoted

    candidate = {"a": TrajectoryScore(45, 0), "b": TrajectoryScore(66, 0)}
    outcome = soft_hurt_gate(candidate, incumbent, max_hurt=0)
    assert outcome.hurt == ()
    assert outcome.helped == ("b",)
    assert outcome.net_gain == 0.5
    assert outcome.promoted


def test_soft_hurt_evaluates_budget_help_margin_and_only_one_repair():
    incumbent = _batch(a=50, b=50)
    candidate = _batch(a=55, b=48)
    margin_tie = soft_hurt_gate(candidate, incumbent, noise_floor=1)
    assert margin_tie.required_gain == margin_tie.net_gain == 1.5
    assert margin_tie.reasons == (GateReason.INSUFFICIENT_GAIN,)
    assert margin_tie.repair_eligible

    repaired = soft_hurt_gate(candidate, incumbent, noise_floor=1, repaired=True)
    assert repaired.required_gain == 1
    assert repaired.promoted
    assert not repaired.repair_eligible

    rejected_repair = soft_hurt_gate(
        candidate, incumbent, noise_floor=1, max_hurt=0, min_help_when_hurt=2, repaired=True,
    )
    assert rejected_repair.reasons == (GateReason.HURT_BUDGET, GateReason.INSUFFICIENT_HELP)
    assert not rejected_repair.repair_eligible

    only_damage = soft_hurt_gate(_batch(a=50, b=33), incumbent)
    assert not only_damage.repair_eligible
    too_many = soft_hurt_gate(_batch(a=40, b=40), incumbent, noise_floor=1, max_hurt=0)
    assert GateReason.HURT_BUDGET in too_many.reasons
    assert not too_many.repair_eligible

    with pytest.raises(ValueError, match="same nonempty"):
        soft_hurt_gate(_batch(a=50), incumbent)
