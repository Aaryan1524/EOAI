"""Post-hoc analysis for the pre-registered comparison (run only after a trial's arms close).

Reads trial and dev-eval directories plus the private expectations. Nothing here is
imported by any adaptation component.
"""

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from experiments.eoai_eval.scoring import score
from experiments.guard.math import GateReason, TrajectoryScore, average_scores, soft_hurt_gate
from experiments.guard.loop import Policy

from .runner import ARMS, POLICY, load_private

VETO = GateReason.REQUIRED_ACTION_INCOMPLETE.value
UNRELIABLE = GateReason.UNRELIABLE_REVIEW.value


def _json(path: Path):
    return json.loads(path.read_text())


def reviews(directory: Path) -> list[dict]:
    return [_json(p) | {"_file": p.name} for p in sorted((directory / "reviews").glob("*.json"))]


def judgment_stats(rows: list[dict]) -> dict:
    semantic = [c for r in rows for c in r["review"]["criteria"] if c.get("semantic_attempts")]
    unresolved = [c for c in semantic if c["passed"] is None]
    retried = [c for c in semantic if len(c["semantic_attempts"]) > 1]
    errors = Counter(a["error"] for c in semantic for a in c["semantic_attempts"] if a["error"])
    return {"rollouts": len(rows), "semantic_judgments": len(semantic), "unresolved": len(unresolved),
            "unresolved_rate": len(unresolved) / len(semantic) if semantic else None,
            "retried": len(retried), "attempt_errors": dict(errors),
            "unresolved_items": [{"file": r["_file"], "criterion": c["id"]} for r in rows
                                 for c in r["review"]["criteria"]
                                 if c.get("semantic_attempts") and c["passed"] is None]}


def oracle_for(rows, cases):
    out = []
    for r in rows:
        case = cases.get(r["task_id"])
        if case is None:
            continue
        report = score(case.snapshot.path, Path(r["sandbox"]), Path(r["trace"]), case.expectations)
        write = next((c["passed"] for c in r["review"]["criteria"] if c["id"] == "eoai_required_action_completion"),
                     "n/a")
        out.append({"file": r["_file"], "task": r["task_id"], "self_pass": r["score"]["strict"] == 100.0,
                    "self_continuous": r["score"]["continuous"], "oracle_pass": bool(report["passed"]),
                    "self_required_write": write})
    return out


def confusion(rows) -> dict:
    tp = sum(r["self_pass"] and r["oracle_pass"] for r in rows)
    fp = sum(r["self_pass"] and not r["oracle_pass"] for r in rows)
    fn = sum(not r["self_pass"] and r["oracle_pass"] for r in rows)
    tn = sum(not r["self_pass"] and not r["oracle_pass"] for r in rows)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "n": len(rows),
            "accuracy": (tp + tn) / len(rows) if rows else None,
            "false_positive_rate": fp / (fp + tn) if fp + tn else None,
            "false_negative_rate": fn / (fn + tp) if fn + tp else None}


def _batch_scores(rows_by_file, prefix, tasks):
    out = {}
    for t in tasks:
        rs = [r for f, r in rows_by_file.items() if f.startswith(f"{prefix}-{t}-")]
        if rs:
            out[t] = average_scores([TrajectoryScore(**r["score"]) for r in rs])
    return out


def gate_audit(trial: Path, arm: str, oracle: dict) -> list[dict]:
    """Per gate call: paper Soft-HURT decision, actual decision, incumbent write failures, oracle change."""
    arm_dir = trial / arm
    if not any(arm_dir.glob("r*-decision.json")):
        return []
    protocol = _json(trial / "protocol.json")
    training, replay = tuple(protocol["training"]), tuple(protocol["replay"])
    rows = {r["_file"]: r for r in reviews(trial / "shared") + reviews(arm_dir)}
    incumbent_prefix = "v0"
    out = []
    for decision_path in sorted(arm_dir.glob("r*-decision.json")):
        d = _json(decision_path)
        label = decision_path.name.split("-")[0]
        record = {"round": d["round"], "decision": d["decision"], "actual_reasons": d["gate"].get("reasons"),
                  "actual_promoted": d["gate"].get("promoted")}
        if d.get("winner") is None:
            record["paper_promoted"] = None
            record["note"] = "round blocked before selection (unreliable candidate batch)"
            out.append(record)
            continue
        winner_index = next(i for i, row in enumerate(d["candidate_pool"]) if row["artifact"] == d["winner"])
        cand = _batch_scores(rows, f"{label}-c{winner_index}", training) | _batch_scores(rows, f"{label}-replay", replay)
        inc = _batch_scores(rows, incumbent_prefix, training + replay)
        tasks = tuple(t for t in training + replay if t in cand and t in inc)
        paper = soft_hurt_gate(cand, inc, noise_floor=POLICY.noise_floor, max_hurt=POLICY.max_hurt,
                               min_gain=POLICY.min_gain, repair_budget=POLICY.repair_budget)
        def write_fail(prefix, t):
            return any(c["id"] == "eoai_required_action_completion" and c["passed"] is False
                       for f, r in rows.items() if f.startswith(f"{prefix}-{t}-") for c in r["review"]["criteria"])
        veto_tasks = [t for t in tasks if write_fail(f"{label}-c{winner_index}" if t in training else f"{label}-replay", t)]
        def oracle_rate(prefix, t):
            vals = [o["oracle_pass"] for o in oracle.values() if o["file"].startswith(f"{prefix}-{t}-")]
            return sum(vals) / len(vals) if vals else None
        oracle_delta = {}
        for t in tasks:
            after = oracle_rate(f"{label}-c{winner_index}" if t in training else f"{label}-replay", t)
            before = oracle_rate(incumbent_prefix, t)
            if after is not None and before is not None and after != before:
                oracle_delta[t] = after - before
        record |= {
            "paper_promoted": paper.promoted, "paper_reasons": list(paper.reasons),
            "paper_helped": list(paper.helped), "paper_hurt": list(paper.hurt), "paper_net_gain": paper.net_gain,
            "veto_tasks": veto_tasks,
            "incumbent_also_failed_write": {t: write_fail(incumbent_prefix, t) for t in veto_tasks},
            "veto_only_rejection": (not d["gate"].get("promoted")) and paper.promoted and bool(veto_tasks),
            "oracle_task_changes": oracle_delta,
            "oracle_net_change": sum(oracle_delta.values()),
        }
        out.append(record)
        if d["decision"] in ("accept", "repair"):
            incumbent_prefix = f"{label}-repair" if d["decision"] == "repair" else None
            if incumbent_prefix is None:
                # The promoted batch is winner training + replay rollouts.
                incumbent_prefix = f"{label}-c{winner_index}"
                for t in replay:
                    for f in list(rows):
                        if f.startswith(f"{label}-replay-{t}-"):
                            rows[f.replace(f"{label}-replay", incumbent_prefix, 1)] = rows[f]
    return out


def permutation_test(diffs: list[float], rounds: int = 100_000, seed: int = 0) -> float:
    observed = abs(sum(diffs))
    nonzero = [d for d in diffs if d]
    if not nonzero:
        return 1.0
    if len(nonzero) <= 20:
        total = hits = 0
        for mask in range(1 << len(nonzero)):
            s = sum(d if mask >> i & 1 else -d for i, d in enumerate(nonzero))
            hits += abs(s) >= observed - 1e-12
            total += 1
        return hits / total
    rng = random.Random(seed)
    hits = sum(abs(sum(d if rng.random() < 0.5 else -d for d in nonzero)) >= observed - 1e-12 for _ in range(rounds))
    return (hits + 1) / (rounds + 1)


def bootstrap_ci(diffs, resamples=10_000, seed=0):
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(resamples))
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def deveval_summary(deveval_dirs: list[Path], families: dict) -> dict:
    per_arm_task = defaultdict(lambda: defaultdict(list))
    per_trial = []
    for directory in deveval_dirs:
        ev = _json(directory / "evaluation.json")
        digests = ev["harness_digests"]
        trial = {}
        for arm, digest in digests.items():
            rows = ev["results"][digest]
            passes = {}
            for row in rows:
                passes.setdefault(row["case_id"], []).append(bool(row["passed"]))
            for case, vals in passes.items():
                per_arm_task[arm][case].append(sum(vals) / len(vals))
            trial[arm] = {"pass_rate": sum(sum(v) / len(v) for v in passes.values()) / len(passes),
                          "passes": {c: all(v) for c, v in passes.items()}, "digest": digest,
                          "shared_with_frozen": digest == digests["frozen"] and arm != "frozen"}
        frozen = trial["frozen"]["passes"]
        for arm, info in trial.items():
            if arm == "frozen":
                continue
            info["newly_broken"] = sorted(c for c in frozen if frozen[c] and not info["passes"][c])
            info["recovered"] = sorted(c for c in frozen if not frozen[c] and info["passes"][c])
        per_trial.append({"dir": str(directory), "arms": {a: {k: v for k, v in i.items() if k != "passes"}
                                                          for a, i in trial.items()}})
    tasks = sorted(per_arm_task["frozen"])
    mean = {arm: {c: sum(v) / len(v) for c, v in by.items()} for arm, by in per_arm_task.items()}
    comparisons = {}
    for a, b in (("guard", "frozen"), ("naive", "frozen"), ("guard_noveto", "frozen"), ("guard", "naive"),
                 ("guard", "guard_noveto")):
        if a in mean and b in mean:
            diffs = [mean[a][c] - mean[b][c] for c in tasks]
            comparisons[f"{a}-{b}"] = {"mean_diff": sum(diffs) / len(diffs), "p_permutation": permutation_test(diffs),
                                       "ci95_bootstrap": bootstrap_ci(diffs)}
    by_family = {}
    for arm, by in mean.items():
        fam = defaultdict(list)
        for c, v in by.items():
            fam[families.get(c, "?")].append(v)
        by_family[arm] = {f: sum(v) / len(v) for f, v in sorted(fam.items())}
    return {"per_trial": per_trial, "task_mean_pass_rate": {a: sum(m.values()) / len(m) for a, m in mean.items()},
            "comparisons": comparisons, "family_pass_rate": by_family}


def cost(directory: Path) -> dict:
    metrics = _json(directory / "role-metrics.json") if (directory / "role-metrics.json").exists() else []
    rows = reviews(directory) if (directory / "reviews").exists() else []
    by = Counter()
    for m in metrics:
        key = f"{m['role']}/{m['purpose']}"
        by[key + ":calls"] += 1
        by[key + ":tokens"] += (m["input_tokens"] or 0) + (m["output_tokens"] or 0)
        by[key + ":seconds"] += m["latency_seconds"]
        by[f"status:{m['status']}"] += 1
    solver_tokens = sum(r["metrics"].get("input_tokens", 0) + r["metrics"].get("output_tokens", 0) for r in rows)
    saturated = [m for m in metrics if (m.get("input_tokens") or 0) >= 30_000]
    return {"role": dict(by), "role_calls": len(metrics), "context_saturation_calls": len(saturated),
            "max_role_input_tokens": max((m.get("input_tokens") or 0 for m in metrics), default=0),
            "role_tokens": sum((m["input_tokens"] or 0) + (m["output_tokens"] or 0) for m in metrics),
            "role_seconds": sum(m["latency_seconds"] for m in metrics),
            "solver_rollouts": len(rows), "solver_tokens": solver_tokens,
            "solver_seconds": sum(r["metrics"].get("elapsed_seconds", 0) for r in rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, default=Path.home() / "EOAI-private" / "comparative-v1")
    parser.add_argument("--trial", type=Path, nargs="+", required=True)
    parser.add_argument("--deveval", type=Path, nargs="*", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    suites = load_private(args.private)
    cases = {c.id: c for s in suites.values() for c in s.cases}
    families = {}
    for split in _json(args.private / "index.json")["splits"].values():
        families |= split["families"]
    report = {"trials": []}
    for trial in args.trial:
        shared = reviews(trial / "shared")
        entry = {"dir": str(trial), "shared_v0": {"judgments": judgment_stats(shared), "cost": cost(trial / "shared")}}
        oracle_shared = {o["file"]: o for o in oracle_for(shared, cases)}
        entry["shared_v0"]["required_write_failures"] = sorted(
            o["task"] for o in oracle_shared.values() if o["self_required_write"] is False)
        all_judgments = list(shared)
        for arm in ARMS:
            arm_dir = trial / arm
            if not arm_dir.exists():
                continue
            rows = reviews(arm_dir)
            all_judgments += rows
            oracle = oracle_shared | {o["file"]: o for o in oracle_for(rows, cases)}
            decisions = [_json(p) for p in sorted(arm_dir.glob("r*-decision.json"))]
            entry[arm] = {
                "status": _json(arm_dir / "status.json"),
                "decisions": [{"round": d["round"], "decision": d["decision"],
                               "reasons": d["gate"].get("reasons") if d.get("gate") else None} for d in decisions],
                "judgments": judgment_stats(rows), "cost": cost(arm_dir),
                "self_review_vs_oracle": confusion(list(oracle.values())),
                "gate_audit": gate_audit(trial, arm, oracle) if arm != "naive" else None,
            }
        totals = judgment_stats(all_judgments)
        entry["trial_unresolved_rate"] = totals["unresolved_rate"]
        entry["evaluable"] = totals["unresolved_rate"] is not None and totals["unresolved_rate"] <= 0.10
        report["trials"].append(entry)
    if args.deveval:
        report["deveval"] = deveval_summary(args.deveval, families)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True, default=str))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
