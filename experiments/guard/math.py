"""Deterministic execution-scoring equations from GUARD §4 and Appendix B."""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from statistics import fmean, pstdev


@dataclass(frozen=True)
class Verdict:
    criterion_id: str
    passed: bool | None
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.criterion_id:
            raise ValueError("criterion_id must be nonempty")
        if self.passed is not None and not isinstance(self.passed, bool):
            raise ValueError("verdicts must be binary or None for ungraded criteria")


@dataclass(frozen=True)
class TrajectoryScore:
    continuous: float
    strict: float

    def __post_init__(self) -> None:
        if not 0 <= self.continuous <= 100 or not 0 <= self.strict <= 100:
            raise ValueError("trajectory scores must be between 0 and 100")


class CriterionStatus(StrEnum):
    REGRESSED = "regressed"
    UNSOLVED_GAP = "unsolved_gap"
    MASTERED = "mastered"


@dataclass(frozen=True)
class CriterionPriority:
    criterion_id: str
    incumbent_obedience: float
    previous_obedience: float | None
    regression_candidate_obedience: float | None
    hurt: float
    gap: float
    priority: float
    status: CriterionStatus


@dataclass(frozen=True)
class TrajectoryContrast:
    best_index: int
    worst_index: int
    strict_delta: float
    continuous_delta: float


@dataclass(frozen=True)
class CandidateFitness:
    index: int
    mean_score: TrajectoryScore
    performance: float
    helped: tuple[str, ...]
    hurt: tuple[str, ...]
    reward: float
    standardized: float


@dataclass(frozen=True)
class CandidatePool:
    fitness: tuple[CandidateFitness, ...]
    best_index: int
    worst_index: int
    mean_reward: float
    reward_scale: float


class GateReason(StrEnum):
    UNRELIABLE_REVIEW = "unreliable_review"
    REQUIRED_ACTION_INCOMPLETE = "required_action_incomplete"
    HURT_BUDGET = "hurt_budget"
    INSUFFICIENT_HELP = "insufficient_help"
    INSUFFICIENT_GAIN = "insufficient_gain"


@dataclass(frozen=True)
class GateDecision:
    promoted: bool
    repair_eligible: bool
    helped: tuple[str, ...]
    hurt: tuple[str, ...]
    net_gain: float | None
    hurt_penalty: float | None
    required_gain: float | None
    reasons: tuple[GateReason, ...]


def _graded_verdicts(verdicts: Iterable[Verdict]) -> dict[str, bool]:
    graded: dict[str, bool] = {}
    seen: set[str] = set()
    for verdict in verdicts:
        if verdict.criterion_id in seen:
            raise ValueError(f"duplicate criterion verdict: {verdict.criterion_id}")
        seen.add(verdict.criterion_id)
        if verdict.passed is not None:
            graded[verdict.criterion_id] = verdict.passed
    return graded


def score_trajectory(
    verdicts: Iterable[Verdict], *, blocking_error: bool = False
) -> TrajectoryScore:
    """Apply Eq. (2) to one rollout's binary verdicts."""
    graded = _graded_verdicts(verdicts).values()
    if not graded:
        raise ValueError("at least one criterion must be graded")
    passed = sum(graded)
    continuous = 100 * passed / len(graded)
    if blocking_error:
        strict = 0.0
    elif passed == len(graded):
        strict = 100.0
    else:
        strict = continuous / 2
    return TrajectoryScore(continuous=continuous, strict=strict)


def average_scores(scores: Iterable[TrajectoryScore]) -> TrajectoryScore:
    """Average scores over a task's replicates or over a batch's tasks."""
    scores = tuple(scores)
    if not scores:
        raise ValueError("at least one score is required")
    return TrajectoryScore(
        continuous=fmean(score.continuous for score in scores),
        strict=fmean(score.strict for score in scores),
    )


def _obedience(rollouts: Iterable[Iterable[Verdict]]) -> dict[str, float]:
    grades: dict[str, list[bool]] = defaultdict(list)
    for verdicts in rollouts:
        for criterion_id, passed in _graded_verdicts(verdicts).items():
            grades[criterion_id].append(passed)
    return {criterion_id: fmean(passed) for criterion_id, passed in grades.items()}


def rank_criteria(
    incumbent: Iterable[Iterable[Verdict]],
    previous: Iterable[Iterable[Verdict]] = (),
    *,
    regression_candidate: Iterable[Iterable[Verdict]] | None = None,
) -> tuple[CriterionPriority, ...]:
    """Apply Eqs. (3)–(4) to graded replicates of the same task.

    Supply a rejected winner's (or rejected repair's) grades to measure hurt
    from incumbent to rejected candidate. Gap remains incumbent-based.
    """
    current = _obedience(incumbent)
    earlier = _obedience(previous)
    rejected = _obedience(regression_candidate) if regression_candidate is not None else {}
    rankings = []
    for criterion_id, obedience in current.items():
        previous_obedience = earlier.get(criterion_id)
        rejected_obedience = rejected.get(criterion_id)
        if regression_candidate is not None:
            hurt = max(0.0, obedience - rejected_obedience) if rejected_obedience is not None else 0.0
        else:
            hurt = max(0.0, previous_obedience - obedience) if previous_obedience is not None else 0.0
        gap = 1.0 - obedience
        if hurt > 0:
            status = CriterionStatus.REGRESSED
        elif gap > 0:
            status = CriterionStatus.UNSOLVED_GAP
        else:
            status = CriterionStatus.MASTERED
        rankings.append(
            CriterionPriority(
                criterion_id=criterion_id,
                incumbent_obedience=obedience,
                previous_obedience=previous_obedience,
                regression_candidate_obedience=rejected_obedience,
                hurt=hurt,
                gap=gap,
                priority=hurt + gap,
                status=status,
            )
        )
    return tuple(
        sorted(rankings, key=lambda item: (-item.priority, -item.hurt, item.criterion_id))
    )


def _check_nonnegative(value: float, label: str) -> None:
    if not isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")


def trajectory_contrast(
    scores: Sequence[TrajectoryScore], *, threshold: float = 10.0
) -> TrajectoryContrast | None:
    """Select a task's top and bottom trajectories using Eq. (5)."""
    _check_nonnegative(threshold, "threshold")
    if len(scores) < 2:
        return None
    ordered = sorted(
        range(len(scores)), key=lambda index: (scores[index].strict, scores[index].continuous)
    )
    worst_index, best_index = ordered[0], ordered[-1]
    best, worst = scores[best_index], scores[worst_index]
    strict_delta = best.strict - worst.strict
    continuous_delta = best.continuous - worst.continuous
    if strict_delta > 0 or (continuous_delta > 0 and continuous_delta >= threshold):
        return TrajectoryContrast(best_index, worst_index, strict_delta, continuous_delta)
    return None


@dataclass(frozen=True)
class _TaskComparison:
    deltas: dict[str, float]
    helped: tuple[str, ...]
    hurt: tuple[str, ...]


def _compare_tasks(
    candidate: Mapping[str, TrajectoryScore],
    incumbent: Mapping[str, TrajectoryScore],
    noise_floor: float,
) -> _TaskComparison:
    if not candidate or candidate.keys() != incumbent.keys():
        raise ValueError("candidate and incumbent must score the same nonempty task batch")
    deltas = {
        task_id: candidate[task_id].continuous - incumbent[task_id].continuous
        for task_id in sorted(candidate)
    }
    return _TaskComparison(
        deltas=deltas,
        helped=tuple(task_id for task_id, delta in deltas.items() if delta > noise_floor),
        hurt=tuple(task_id for task_id, delta in deltas.items() if delta < -noise_floor),
    )


def candidate_fitness(
    candidates: Sequence[Mapping[str, TrajectoryScore]],
    incumbent: Mapping[str, TrajectoryScore],
    *,
    noise_floor: float = 15.0,
    hurt_weight: float = 3.5,
    help_weight: float = 1.5,
) -> CandidatePool:
    """Score a candidate pool and select the contrast endpoints (Eqs. 7–9).

    For equal reward, prefer higher continuous score, then fewer hurt tasks,
    then earlier input index. The worst endpoint reverses this order.
    """
    if len(candidates) < 2:
        raise ValueError("candidate pool must contain at least two candidates")
    _check_nonnegative(noise_floor, "noise_floor")
    _check_nonnegative(hurt_weight, "hurt_weight")
    _check_nonnegative(help_weight, "help_weight")
    if hurt_weight <= help_weight:
        raise ValueError("hurt_weight must exceed help_weight")

    scores = [average_scores(candidate.values()) for candidate in candidates]
    comparisons = [_compare_tasks(candidate, incumbent, noise_floor) for candidate in candidates]
    performances = [score.strict + 0.25 * score.continuous for score in scores]
    rewards = [
        performance - hurt_weight * len(comparison.hurt) + help_weight * len(comparison.helped)
        for performance, comparison in zip(performances, comparisons, strict=True)
    ]
    mean_reward = fmean(rewards)
    reward_scale = pstdev(rewards) + 1e-6
    fitness = tuple(
        CandidateFitness(
            index=index,
            mean_score=score,
            performance=performance,
            helped=comparison.helped,
            hurt=comparison.hurt,
            reward=reward,
            standardized=(reward - mean_reward) / reward_scale,
        )
        for index, (score, performance, comparison, reward) in enumerate(
            zip(scores, performances, comparisons, rewards, strict=True)
        )
    )
    ordered = sorted(
        fitness,
        key=lambda item: (-item.reward, -item.mean_score.continuous, len(item.hurt), item.index),
    )
    return CandidatePool(fitness, ordered[0].index, ordered[-1].index, mean_reward, reward_scale)


def soft_hurt_gate(
    candidate: Mapping[str, TrajectoryScore],
    incumbent: Mapping[str, TrajectoryScore],
    *,
    noise_floor: float = 15.0,
    max_hurt: int = 1,
    min_gain: float = 1.5,
    repair_budget: int = 1,
    min_help_when_hurt: int = 1,
    repaired: bool = False,
) -> GateDecision:
    """Apply Eqs. (11)–(13) to paired continuous task scores."""
    _check_nonnegative(noise_floor, "noise_floor")
    _check_nonnegative(min_gain, "min_gain")
    if max_hurt < 0 or repair_budget < 0 or min_help_when_hurt < 1:
        raise ValueError("hurt budgets must be nonnegative and minimum help must be positive")

    comparison = _compare_tasks(candidate, incumbent, noise_floor)
    net_gain = fmean(comparison.deltas.values())
    hurt = comparison.hurt
    hurt_penalty = sum(-comparison.deltas[task_id] for task_id in hurt) / len(comparison.deltas)
    margin = min_gain if hurt and not repaired else 0.0
    required_gain = max(hurt_penalty, margin)
    reasons = []
    if len(hurt) > max_hurt:
        reasons.append(GateReason.HURT_BUDGET)
    if hurt and len(comparison.helped) < min_help_when_hurt:
        reasons.append(GateReason.INSUFFICIENT_HELP)
    if net_gain <= required_gain:
        reasons.append(GateReason.INSUFFICIENT_GAIN)

    # Check whether removing the isolated regressions would clear the gate.
    repair_eligible = (
        not repaired and bool(reasons) and 1 <= len(hurt) <= repair_budget
        and net_gain + hurt_penalty > 0
    )
    return GateDecision(
        promoted=not reasons,
        repair_eligible=repair_eligible,
        helped=comparison.helped,
        hurt=hurt,
        net_gain=net_gain,
        hurt_penalty=hurt_penalty,
        required_gain=required_gain,
        reasons=tuple(reasons),
    )
