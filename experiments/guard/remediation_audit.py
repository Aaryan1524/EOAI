"""Read-only counterfactual over the public Phase 1 pilot."""

import argparse
import json
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.tools.gateway import declarations_for_profile
from experiments.eoai_eval.execution import ROOT, sha_file, verify_trial_artifacts

from .grounding import validate_rule
from .loop import Policy, Trajectory, paired_scores, promotion_gate, write_json
from .math import Verdict, soft_hurt_gate
from .review import Checklist, Criterion, _fixed_checks, review_rollout


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fingerprint(root: Path) -> str:
    files = {p.relative_to(root).as_posix(): sha_file(p) for p in sorted(root.rglob("*")) if p.is_file()}
    return sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _legacy_verdict(row: dict) -> Verdict:
    # Pilot-06 did not retain raw replies. Historical valid grades are reused as
    # observations; invalid grades cannot be revalidated and become unknown.
    why = row["why"]
    grade = None if why.startswith("Reviewer failed to return") else row["passed"]
    return Verdict(row["id"], grade, why)


def audit_archive(source: Path, reviewer=None) -> dict:
    fingerprint = _fingerprint(source)
    protocol = _load(source / "protocol.json")
    independent = _load(source / "independent-development-audit.json")["trials"]
    original = _load(source / "r1-decision.json")
    policy = Policy(**protocol["policy"])
    tasks = tuple(protocol["training"] + protocol["replay"])
    known = {}
    deterministic_scores = {}
    corrected_scores = {}
    live_scores, live_reviews = {}, {}
    metrics = Counter()
    winner = original["fitness"]["best_index"]
    selected_trials = {
        f"v0-{task}-1" for task in tasks
    } | {
        f"r1-c{winner}-{task}-1" if task in protocol["training"] else f"r1-replay-{task}-1"
        for task in tasks
    }
    for path in sorted((source / "reviews").glob("*.json")):
        prior = _load(path)
        name, trace_key = prior["task_id"], prior["trace_key"]
        if name not in tasks:
            raise ValueError("Unexpected task in archived pilot")
        task = load_task(name, authorize_writes=True).task
        snapshot = source / "snapshots" / f"{name}.sqlite3"
        trace = source / "traces" / trace_key
        database = source / "sandboxes" / trace_key / "run.sqlite3"
        verify_trial_artifacts(trace, database, _load(trace / "experiment.json"))
        frozen = _load(source / "checklists" / f"{name}.json")
        fixed = tuple(_fixed_checks(frozen["public_context"], frozen["request"]))
        checklist = Checklist(frozen["request"], json.dumps(frozen["public_context"]),
                              json.dumps(frozen["tool_declarations"]), fixed)
        trial = SimpleNamespace(trace_path=trace, sandbox=SimpleNamespace(path=database))
        deterministic = review_rollout(checklist, trial, snapshot, None, task=task)
        observed = {row["id"]: row for row in prior["review"]["criteria"]}
        archived_semantic = tuple(_legacy_verdict(observed[row["id"]]) for row in frozen["criteria"]
                                  if row["hook"] is None)
        fixes = tuple(Verdict(row.id, row.passed, row.why) for row in deterministic.criteria)
        terminal = deterministic.blocking_error is not None
        deterministic_scores[path.stem] = Trajectory(name, trace_key, fixes, terminal, {}, {})
        corrected_scores[path.stem] = Trajectory(name, trace_key, fixes + archived_semantic, terminal, {}, {})
        old, new = prior["score"], corrected_scores[path.stem].score
        success = independent[path.stem]["passed"]
        metrics["old_perfect"] += old["strict"] == 100
        metrics["old_perfect_false_pass"] += old["strict"] == 100 and not success
        comparable = all(row.passed is not None for row in archived_semantic)
        metrics["corrected_comparable"] += comparable
        metrics["corrected_perfect"] += comparable and new.strict == 100
        metrics["corrected_perfect_false_pass"] += comparable and new.strict == 100 and not success
        metrics["old_invalid_verdicts"] += sum(row.passed is None for row in archived_semantic)
        metrics["semantic_verdicts"] += len(archived_semantic)
        old_complete = next((row for row in fixes if row.criterion_id == "eoai_required_action_completion"), None)
        known[path.stem] = {
            "task_id": name, "independent_pass": success, "archived": old,
            "corrected_review_comparable": comparable,
            "corrected_known_criteria_score": asdict(new),
            "deterministic_only_score": asdict(deterministic_scores[path.stem].score),
            "required_action_completion": asdict(old_complete) if old_complete is not None else None,
        }
        if reviewer is not None and path.stem in selected_trials:
            semantics = tuple(Criterion(**row) for row in frozen["criteria"] if row["hook"] is None)
            live = review_rollout(Checklist(checklist.request, checklist.context_json, checklist.declarations_json,
                                           fixed + semantics), trial, snapshot, reviewer, task=task)
            live_reviews[path.stem] = live.as_dict()
            live_scores[path.stem] = Trajectory(
                name, trace_key, tuple(Verdict(row.id, row.passed, row.why) for row in live.criteria),
                live.blocking_error is not None, {}, {},
            )

    def pair(rows):
        before = {task: (rows[f"v0-{task}-1"],) for task in tasks}
        after = {task: (rows[f"r1-c{winner}-{task}-1" if task in protocol["training"]
                             else f"r1-replay-{task}-1"],) for task in tasks}
        return after, before

    fixed_pair = pair(deterministic_scores)
    raw_fixed = soft_hurt_gate(
        *(paired_scores(batch, tasks) for batch in fixed_pair),
        noise_floor=policy.noise_floor, max_hurt=policy.max_hurt,
        min_gain=policy.min_gain, repair_budget=policy.repair_budget,
    )
    tools = [tool for profile in ("enterprise_ops:v1", "enterprise_ops:v2")
             for tool in declarations_for_profile(profile)]
    proposal_checks = {
        path.name: validate_rule(_load(path)["edit"]["new"], tools, forbidden=tasks)
        for path in sorted(source.glob("r1-candidate-*.json"))
    }
    if _fingerprint(source) != fingerprint:
        raise RuntimeError("Archive changed while reading")
    report = {
        "archive_sha256": fingerprint, "archive_unchanged": True, "trial_count": len(known),
        "calibration": dict(metrics), "trials": known, "original_gate": original["gate"],
        "corrected_gate": asdict(promotion_gate(*pair(corrected_scores), tasks, policy)),
        "deterministic_only_gate": asdict(promotion_gate(*fixed_pair, tasks, policy)),
        "pure_math_deterministic_only_counterfactual": asdict(raw_fixed),
        "archived_proposal_rejections": proposal_checks,
    }
    if reviewer is not None:
        semantic_rows = [row for review in live_reviews.values() for row in review["criteria"]
                         if row["semantic_attempts"]]
        report["live_review"] = {
            "gate": asdict(promotion_gate(*pair(live_scores), tasks, policy)),
            "semantic_criteria": len(semantic_rows),
            "old_unknown_on_same_pairs": sum(
                row.passed is None for key in selected_trials for row in corrected_scores[key].verdicts
            ),
            "unresolved": sum(row["passed"] is None for row in semantic_rows),
            "retries": sum(len(row["semantic_attempts"]) > 1 for row in semantic_rows),
            "reviews": live_reviews, "role_metrics": reviewer.metrics,
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--live-review", action="store_true", help="Re-review only the ten archived gate runs")
    args = parser.parse_args()
    source = ROOT / "runs" / "guard-phase1" / "pilot-06"
    output = args.out.resolve()
    if not output.is_relative_to(ROOT / "runs") or output.is_relative_to(source):
        parser.error("Choose a new output below runs and outside the original pilot")
    output.parent.mkdir(parents=True, exist_ok=True)
    reviewer = None
    if args.live_review:
        from .__main__ import MODEL_SHA256, _safety_probe
        from .roles import RoleModel

        _safety_probe()
        reviewer = RoleModel(MODEL_SHA256)
    write_json(output, audit_archive(source, reviewer))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
