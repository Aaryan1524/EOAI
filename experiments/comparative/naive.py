"""Naive recursive self-improvement (Arm B).

Each round reflects on the incumbent's failed adaptation tasks and proposes
``pool_size`` revisions. The best candidate by mean continuous training score
is always promoted. There is no regret ranking, no contrast evolution, no
rejection ledger, no replay buffer and no Soft-HURT or veto gate.

To isolate GUARD's selection and gating mechanism, the arm deliberately shares
with GUARD: the solver and self-review (same checklists and trace-derived
failure text), the proposer prompt (``loop.DISTILL``), the scoped single-rule
edit format and the grounding filter (``loop.Edit.from_proposal`` via
``loop._proposal``), pool size, rounds and replicates.
"""

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from enterprise_ops.agent.harness import Harness

from experiments.guard.grounding import compact_declarations
from experiments.guard.loop import (
    SHARED_STRATEGY, Batch, Complete, Policy, Rollout, _artifact, _is_grade, _proposal, content, exact_diff,
    paired_scores, write_json,
)

# Same initial strategy text as GUARD; Naive never evolves it.
NAIVE_STRATEGY = SHARED_STRATEGY


def reflection(incumbent: Batch, tasks: tuple[str, ...], descriptions: Mapping[str, Mapping[str, str]]) -> dict:
    """Flat, unranked failure evidence plus observed traces of every training task (as GUARD supplies)."""
    failures = []
    observations = [{"task": task, "trace": run.excerpt} for task in tasks for run in incumbent[task][:2]]
    for task in tasks:
        runs = incumbent[task]
        failed = {}
        for run in runs:
            for row in run.verdicts:
                if _is_grade(row) and row.passed is False:
                    failed.setdefault(row.criterion_id, []).append(row.rationale[:280])
        if not failed and not any(run.blocking_error for run in runs):
            continue
        failures.append({
            "task": task,
            "failed_requirements": [
                {"requirement": descriptions[task][criterion], "failure_evidence": list(dict.fromkeys(why))[:2]}
                for criterion, why in sorted(failed.items())
            ],
            "blocking_error": any(run.blocking_error for run in runs),
        })
    return {"failed_tasks": failures, "observed_incumbent_traces": observations,
            "note": None if failures else "Self-review found no failed requirement on any adaptation task."}


def run_naive(
    root: Path, baseline: Harness, training: tuple[str, ...], descriptions: Mapping[str, Mapping[str, str]],
    solve: Rollout, model: Complete, policy: Policy, *, declarations: Sequence[dict], forbidden: Sequence[str] = (),
) -> dict:
    if not training or len(set(training)) != len(training) or set(descriptions) != set(training):
        raise ValueError("Naive RSI requires distinct training tasks with frozen checklists")
    tools = compact_declarations(declarations)
    blocked = (*training, *forbidden, *(key for checks in descriptions.values() for key in checks))
    bundles = root / "harnesses"
    bundles.mkdir()
    current = content(baseline)
    baseline_id = incumbent_id = _artifact(bundles, current)
    incumbent = solve(baseline, "v0", training)
    rounds = []
    for number in range(1, policy.rounds + 1):
        label = f"r{number}"
        evidence = reflection(incumbent, training, descriptions)
        write_json(root / f"{label}-reflection.json", evidence)
        with ThreadPoolExecutor(max_workers=policy.pool_size) as pool:
            futures = [pool.submit(_proposal, model, current, NAIVE_STRATEGY, evidence, tools, blocked)
                       for _ in range(policy.pool_size)]
            proposals = [future.result() for future in futures]
        # Within-pool de-duplication only (no memory of earlier rounds), same retry allowance as GUARD.
        seen = {current.digest}
        for index, (bundle, _) in enumerate(proposals):
            for _ in range(2):
                if bundle.digest not in seen:
                    break
                proposals[index] = _proposal(model, current, NAIVE_STRATEGY, evidence, tools, blocked)
                bundle = proposals[index][0]
            seen.add(bundle.digest)
        rows, batches, means = [], [], []
        for index, (bundle, edit) in enumerate(proposals):
            artifact = _artifact(bundles, bundle)
            diff = exact_diff(current, bundle)
            write_json(root / f"{label}-candidate-{index}.json", {
                "artifact": artifact, "parent": incumbent_id, "edit": asdict(edit), "diff": diff,
                "strategy": NAIVE_STRATEGY,
            })
            measured = solve(bundle, f"{label}-c{index}", training)
            scores = paired_scores(measured, training)
            mean = sum(score.continuous for score in scores.values()) / len(scores)
            rows.append({"artifact": artifact, "diff": diff, "mean_continuous": mean})
            batches.append(measured)
            means.append(mean)
        selected = max(range(len(means)), key=lambda i: (means[i], -i))
        previous = incumbent_id
        current = proposals[selected][0]
        incumbent = batches[selected]
        incumbent_id = rows[selected]["artifact"]
        entry = {"round": number, "incumbent_before": previous, "incumbent_after": incumbent_id,
                 "decision": "accept", "winner": incumbent_id, "candidate_pool": rows,
                 "selection_rule": "max mean continuous self-review score on training tasks; always promote"}
        write_json(root / f"{label}-decision.json", entry)
        rounds.append(entry)
    final = {"baseline": baseline_id, "final": incumbent_id, "rounds": rounds,
             "final_scores": {task: asdict(score) for task, score in paired_scores(incumbent, training).items()}}
    write_json(root / "selection.json", final)
    return final
