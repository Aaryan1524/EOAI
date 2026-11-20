"""Fixed-batch execution-scored GUARD, with no reference grades in the loop."""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from difflib import unified_diff
from pathlib import Path

from enterprise_ops.agent.harness import Harness, HarnessContent
from enterprise_ops.agent.model import strict_json

from .math import (
    TrajectoryScore, Verdict, average_scores, candidate_fitness, rank_criteria, score_trajectory,
    soft_hurt_gate, trajectory_contrast,
)

Complete = Callable[[str, str, str, dict, float], dict]
Fields = ("instructions", "skills", "memory")
SHARED_STRATEGY = (
    "Use the champion's observed traces to identify its main behavioral failure. Propose a single focused "
    "harness mechanism grounded in that diagnosis. Use an explicit observable [iff condition] so it does "
    "not affect tasks the champion handles. Consider a short fix slice and a replay slice before proposing it."
)
DISTILL = (
    "Apply the strategy to the supplied incumbent and evidence. Preserve mastered constraints. "
    "Propose one appended mechanism. Return field, condition, rule, and why as strings. "
    "Choose instructions, skills, or memory for field. Write a short observable condition without brackets. "
    "Write the rule to apply only under that condition. The controller supplies the [iff] wrapper. "
    "Use no executable code. Do not hard-code case IDs, reference outcomes, or tool results."
)


@dataclass(frozen=True)
class Edit:
    field: str
    old: str
    new: str
    why: str

    @classmethod
    def parse(cls, value: dict) -> "Edit":
        if not isinstance(value, dict) or set(value) != {"field", "old", "new", "why"}:
            raise ValueError("Invalid edit shape")
        if any(type(part) is not str for part in value.values()):
            raise ValueError("Edit fields must be text")
        edit = cls(**value)
        if edit.field not in Fields or len(edit.old) > 3000 or not 0 < len(edit.why) <= 1200:
            raise ValueError("Invalid edit field or explanation")
        if not 0 < len(edit.new) <= 2500 or not re.match(r"\s*\[iff [^\[\]\n]{4,250}\]", edit.new):
            raise ValueError("Edit lacks a bounded [iff condition]")
        return edit

    @classmethod
    def from_proposal(cls, value: dict, repair_of: "Edit | None" = None) -> "Edit":
        fields = {"condition", "rule", "why"}
        if repair_of is None:
            fields.add("field")
        if not isinstance(value, dict) or value.keys() != fields or any(type(part) is not str for part in value.values()):
            raise ValueError("Invalid scoped proposal shape")
        condition = value["condition"].strip()
        rule = value["rule"].strip()
        if not rule or not re.fullmatch(r"[^\[\]\n]{4,250}", condition):
            raise ValueError("Proposal requires a bounded condition and nonempty rule")
        return cls.parse({
            "field": value["field"] if repair_of is None else repair_of.field,
            "old": "" if repair_of is None else repair_of.new.strip(),
            "new": f"[iff {condition}] {rule}",
            "why": value["why"],
        })


def content(harness: Harness | HarnessContent) -> HarnessContent:
    return harness if isinstance(harness, HarnessContent) else HarnessContent(harness.instructions)


def apply_edit(harness: HarnessContent, edit: Edit) -> HarnessContent:
    fields = harness.as_dict()
    before = fields[edit.field]
    if edit.old:
        if before.count(edit.old) != 1:
            raise ValueError("Replacement must identify one exact passage")
        after = before.replace(edit.old, edit.new, 1)
    else:
        after = "\n\n".join(part for part in (before, edit.new.strip()) if part)
    if before == after:
        raise ValueError("Edit does not change the harness")
    fields[edit.field] = after
    return HarnessContent(**fields)


def exact_diff(before: HarnessContent, after: HarnessContent) -> dict[str, str]:
    def diff(field: str) -> str:
        lines = unified_diff(
            getattr(before, field).splitlines(keepends=True),
            getattr(after, field).splitlines(keepends=True),
            fromfile=f"before/{field}", tofile=f"after/{field}",
        )
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                       for line in lines)

    return {
        field: diff(field)
        for field in Fields if getattr(before, field) != getattr(after, field)
    }


def write_json(path: Path, data) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


@dataclass(frozen=True)
class Trajectory:
    task_id: str
    trace_key: str
    verdicts: tuple[Verdict, ...]
    blocking_error: bool
    metrics: dict
    excerpt: dict

    @property
    def score(self) -> TrajectoryScore:
        return score_trajectory(self.verdicts, blocking_error=self.blocking_error)

    def record(self) -> dict:
        return asdict(self) | {"score": asdict(self.score)}


@dataclass(frozen=True)
class Policy:
    pool_size: int = 2
    rounds: int = 1
    replicates: int = 1
    noise_floor: float = 15
    max_hurt: int = 1
    min_gain: float = 1.5
    repair_budget: int = 1
    ledger_size: int = 40

    def __post_init__(self):
        if any(type(value) is not int for value in (
            self.pool_size, self.rounds, self.replicates, self.max_hurt, self.repair_budget, self.ledger_size,
        )):
            raise ValueError("Round and buffer limits must be integers")
        if not 2 <= self.pool_size <= 8 or not 1 <= self.rounds <= 10 or not 1 <= self.replicates <= 4:
            raise ValueError("Invalid experiment size")
        if not 1 <= self.ledger_size <= 40 or self.max_hurt < 0 or self.repair_budget < 0:
            raise ValueError("Invalid gate or ledger limits")


Batch = Mapping[str, Sequence[Trajectory]]
Rollout = Callable[[Harness | HarnessContent, str, tuple[str, ...]], Batch]


def paired_scores(batch: Batch, tasks: tuple[str, ...]) -> dict[str, TrajectoryScore]:
    return {task: average_scores([run.score for run in batch[task]]) for task in tasks}


def _paired_verdicts(batch: Batch | None, task: str):
    return [run.verdicts for run in batch.get(task, ())] if batch is not None else ()


def _diagnostics(
    incumbent: Batch, prior: Batch | None, rejected: Batch | None, history: Sequence[Batch],
    tasks: tuple[str, ...], descriptions: Mapping[str, Mapping[str, str]], ledger: Sequence[dict],
) -> dict:
    groups: dict[str, list[dict]] = {"regressed": [], "unsolved_gap": [], "mastered": []}
    contrasts = []
    for task in tasks:
        current = _paired_verdicts(incumbent, task)
        earlier = _paired_verdicts(prior, task)
        refused = _paired_verdicts(rejected, task) if rejected is not None else None
        for item in rank_criteria(current, earlier, regression_candidate=refused):
            rationales = list(dict.fromkeys(
                verdict.rationale for trial in current for verdict in trial
                if verdict.criterion_id == item.criterion_id and verdict.passed is False
            ))[:2]
            groups[str(item.status)].append(asdict(item) | {
                "task": task, "requirement": descriptions[task][item.criterion_id],
                "incumbent_failure_evidence": [reason[:280] for reason in rationales],
            })
        runs = [run for batch in history[-5:] for run in batch.get(task, ())]
        contrast = trajectory_contrast([run.score for run in runs])
        if contrast is not None:
            contrasts.append({
                "task": task, **asdict(contrast),
                "better": runs[contrast.best_index].excerpt,
                "worse": runs[contrast.worst_index].excerpt,
            })
    for items in groups.values():
        items.sort(key=lambda item: (-item["priority"], -item["hurt"], item["task"], item["criterion_id"]))
    return {"criteria": groups, "contrasts": contrasts, "rejections": list(ledger)}


def _proposal(model: Complete, incumbent: HarnessContent, strategy: str, regret: dict) -> tuple[HarnessContent, Edit]:
    payload = {"strategy": strategy, "incumbent": incumbent.as_dict(), "regret": regret}
    for attempt, temperature in enumerate((0.65, 0.85), start=1):
        try:
            instruction = DISTILL if attempt == 1 else DISTILL + " Use short, plain text in each field."
            edit = Edit.from_proposal(model("candidate-generator", "distill", instruction, payload, temperature))
            return apply_edit(incumbent, edit), edit
        except ValueError as error:
            if attempt == 2:
                raise ValueError(f"Candidate could not be generated within its retry budget: {error}") from error
    raise AssertionError("Unreachable proposal state")


def _repair(model: Complete, incumbent: HarnessContent, winner: HarnessContent,
            winning_edit: Edit, gate: dict, regret: dict):
    value = model(
        "distiller", "repair",
        "Narrow the winning rule using the paired examples of the replay regression. Return exactly "
        "condition, rule and why as strings. The controller replaces the winning rule in its original field. "
        "Use a plain observable condition without brackets and no executable code.",
        {"incumbent": incumbent.as_dict(), "candidate": winner.as_dict(), "gate": gate, "regret": regret}, 0.25,
    )
    edit = Edit.from_proposal(value, repair_of=winning_edit)
    return apply_edit(winner, edit), edit


def _evolve(model: Complete, best: dict, worst: dict, tasks: tuple[str, ...], pool_size: int) -> tuple[list[str], list[str]]:
    payload = json.dumps({"best": best, "worst": worst}, ensure_ascii=False)
    for task in sorted(tasks, key=len, reverse=True):
        payload = re.sub(re.escape(task), "<TASK_ID>", payload, flags=re.IGNORECASE)
    value = model(
        "distiller", "contrast",
        "Contrast the winning and worst strategies and edits using the observed metrics. "
        "Explain what differed and rewrite both strategy prompts. The best prompt should emphasize the "
        "useful scoped behavior and the worst prompt should narrow over-broad conditions. Return JSON "
        "with learnings (list of strings) and updated_best_prompt and updated_worst_prompt (strings).",
        strict_json(payload), 0.2,
    )
    if set(value) != {"learnings", "updated_best_prompt", "updated_worst_prompt"}:
        raise ValueError("Invalid contrast response")
    learnings = value["learnings"]
    prompts = [value["updated_best_prompt"], value["updated_worst_prompt"]]
    if (
        not isinstance(learnings, list) or not 0 < len(learnings) <= 8
        or any(type(item) is not str or not 0 < len(item) <= 800 for item in learnings)
        or any(type(prompt) is not str or not 0 < len(prompt) <= 2600 for prompt in prompts)
        or any(task.casefold() in prompt.casefold() for task in tasks for prompt in prompts)
    ):
        raise ValueError("Invalid evolved proposal prompt")
    half = pool_size // 2
    return [prompts[0]] * half + [prompts[1]] * (pool_size - half), learnings


def _artifact(root: Path, bundle: HarnessContent) -> str:
    path = root / f"{bundle.digest}.json"
    if path.exists():
        if strict_json(path.read_bytes()) != bundle.as_dict():
            raise ValueError("Immutable harness artifact differs from its digest")
    else:
        write_json(path, bundle.as_dict())
        path.chmod(0o444)
    return bundle.digest


def run_rounds(
    root: Path, baseline: Harness, training: tuple[str, ...], replay: tuple[str, ...],
    descriptions: Mapping[str, Mapping[str, str]], solve: Rollout, model: Complete, policy: Policy,
) -> dict:
    """Use only scored trajectories produced by self-review to decide promotions."""
    tasks = training + replay
    if not training or len(set(tasks)) != len(tasks) or set(descriptions) != set(tasks):
        raise ValueError("Require disjoint training and replay tasks with frozen checklists")
    bundles = root / "harnesses"
    bundles.mkdir()
    current = content(baseline)
    baseline_artifact = incumbent_id = _artifact(bundles, current)
    incumbent = solve(baseline, "v0", tasks)
    prior = rejected = None
    history = [incumbent]
    ledger: list[dict] = []
    rejected_artifacts: set[str] = set()
    prompts = [SHARED_STRATEGY] * policy.pool_size
    rounds = []
    for number in range(1, policy.rounds + 1):
        label = f"r{number}"
        regret = _diagnostics(incumbent, prior, rejected, history, tasks, descriptions, ledger)
        write_json(root / f"{label}-regret.json", regret)
        with ThreadPoolExecutor(max_workers=policy.pool_size) as pool:
            futures = [pool.submit(_proposal, model, current, prompt, regret) for prompt in prompts]
            proposals = [future.result() for future in futures]
        seen = rejected_artifacts.copy()
        for index, (bundle, _) in enumerate(proposals):
            for _ in range(2):
                if bundle.digest not in seen:
                    break
                proposals[index] = _proposal(model, current, prompts[index], regret)
                bundle = proposals[index][0]
            if bundle.digest in seen:
                raise ValueError("Candidate pool repeats a revision or a previously rejected harness")
            seen.add(bundle.digest)
        rows = []
        evaluated = []
        for index, ((bundle, edit), strategy) in enumerate(zip(proposals, prompts, strict=True)):
            artifact = _artifact(bundles, bundle)
            diff = exact_diff(current, bundle)
            write_json(root / f"{label}-candidate-{index}.json", {
                "artifact": artifact, "parent": incumbent_id, "edit": asdict(edit), "diff": diff,
                "strategy": strategy,
            })
            measured = solve(bundle, f"{label}-c{index}", training)
            evaluated.append(measured)
            rows.append({"artifact": artifact, "diff": diff, "strategy": strategy})
        history.extend(evaluated)
        group = candidate_fitness(
            [paired_scores(measured, training) for measured in evaluated],
            paired_scores(incumbent, training), noise_floor=policy.noise_floor,
        )
        selected = group.best_index
        winner = proposals[selected][0]
        winning_batch = dict(evaluated[selected])
        if replay:
            winning_batch.update(solve(winner, f"{label}-replay", replay))
        history.append(winning_batch)
        incumbent_scores = paired_scores(incumbent, tasks)
        winner_scores = paired_scores(winning_batch, tasks)
        paired_deltas = {
            task: {
                "continuous": winner_scores[task].continuous - incumbent_scores[task].continuous,
                "strict": winner_scores[task].strict - incumbent_scores[task].strict,
                "criteria": [
                    asdict(item) for item in rank_criteria(
                        _paired_verdicts(winning_batch, task), _paired_verdicts(incumbent, task),
                    ) if item.hurt > 0
                ],
            } for task in tasks
        }
        gate = soft_hurt_gate(
            winner_scores, incumbent_scores,
            noise_floor=policy.noise_floor, max_hurt=policy.max_hurt, min_gain=policy.min_gain,
            repair_budget=policy.repair_budget,
        )
        promoted = winner if gate.promoted else None
        winning_id = rows[selected]["artifact"]
        promoted_batch = winning_batch
        repair = None
        if gate.repair_eligible and not gate.promoted:
            repair_evidence = {
                "winning_edit": asdict(proposals[selected][1]), "incumbent_regret": regret,
                "hurt_replays": {
                    task: {"paired_delta": paired_deltas[task], "incumbent": incumbent[task][0].excerpt,
                           "winner": winning_batch[task][0].excerpt}
                    for task in gate.hurt
                },
            }
            try:
                repaired, edit = _repair(model, current, winner, proposals[selected][1], asdict(gate), repair_evidence)
                if repaired.digest in rejected_artifacts:
                    raise ValueError("Scope repair repeats a previously rejected harness")
            except (ValueError, RuntimeError) as error:
                repair = {"error": str(error)}
            else:
                repaired_id = _artifact(bundles, repaired)
                corrected = solve(repaired, f"{label}-repair", tasks)
                history.append(corrected)
                repair_gate = soft_hurt_gate(
                    paired_scores(corrected, tasks), paired_scores(incumbent, tasks),
                    noise_floor=policy.noise_floor, max_hurt=policy.max_hurt,
                    min_gain=policy.min_gain, repair_budget=policy.repair_budget, repaired=True,
                )
                repair = {"artifact": repaired_id, "diff": exact_diff(winner, repaired),
                          "edit": asdict(edit), "gate": asdict(repair_gate)}
                if repair_gate.promoted:
                    promoted, promoted_batch = repaired, corrected
                else:
                    rejected_artifacts.add(repaired_id)
                    winning_batch = corrected
        previous_id = incumbent_id
        if promoted is None:
            rejected_artifacts.add(winning_id)
            rejected = winning_batch
            prior = None
            ledger = [row for row in ledger if row["artifact"] != winning_id]
            ledger.append({"artifact": winning_id, "gate": asdict(gate),
                           "rule": proposals[selected][1].new[:800], "repair": repair})
            ledger = ledger[-policy.ledger_size:]
            write_json(root / f"{label}-rejection-ledger.json", ledger)
        else:
            prior, incumbent, rejected = incumbent, promoted_batch, None
            current = promoted
            incumbent_id = _artifact(bundles, current)
        by_index = {row.index: row for row in group.fitness}

        def contrast_input(index: int) -> dict:
            fit = by_index[index]
            return {"strategy": prompts[index], "rule": proposals[index][1].new,
                    "reward": fit.reward, "helped": len(fit.helped), "hurt": len(fit.hurt)}

        try:
            prompts, learnings = _evolve(
                model, contrast_input(group.best_index), contrast_input(group.worst_index), tasks, policy.pool_size,
            )
            evolution = {"learnings": learnings, "next_prompts": prompts}
        except (ValueError, RuntimeError) as error:
            evolution = {"error": str(error), "retained_prompts": prompts}
        entry = {
            "round": number, "incumbent_before": previous_id, "incumbent_after": incumbent_id,
            "decision": "reject" if promoted is None else "accept" if gate.promoted else "repair",
            "winner": winning_id, "candidate_pool": rows, "fitness": asdict(group), "gate": asdict(gate),
            "paired_task_deltas": paired_deltas,
            "repair": repair, "proposal_evolution": evolution,
        }
        write_json(root / f"{label}-decision.json", entry)
        rounds.append(entry)
    final = {"baseline": baseline_artifact, "final": incumbent_id, "rounds": rounds,
             "final_scores": {task: asdict(score) for task, score in paired_scores(incumbent, tasks).items()}}
    write_json(root / "selection.json", final)
    return final
