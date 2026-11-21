"""Offline check that GUARD's role payloads fit its 128 KiB relay cap for a given task buffer.

Uses recorded trial-1 shared v0 reviews and GUARD's own ``_diagnostics``; no model calls.
Worst case adds a contrast for every task, a full rejection ledger for the rounds run, and the
largest repair payload ``loop._repair`` can build.
"""

import argparse
import json
from pathlib import Path

from enterprise_ops.agent.harness import load_harness
from enterprise_ops.tools.gateway import declarations_for_profile
from experiments.guard import loop
from experiments.guard.grounding import compact_declarations
from experiments.guard.math import Verdict

LIMIT = 128 * 1024


def _trajectories(shared: Path, tasks):
    batch = {}
    for task in tasks:
        rows = sorted((shared / "reviews").glob(f"v0-{task}-*.json"))
        batch[task] = tuple(loop.Trajectory(
            r["task_id"], r["trace_key"], tuple(Verdict(v["criterion_id"], v["passed"], v["rationale"]) for v in r["verdicts"]),
            r["blocking_error"], r["metrics"], r["excerpt"]) for r in (json.loads(p.read_text()) for p in rows))
    return batch


def measure(shared: Path, tasks, profiles, rounds=3):
    checklists = {t: json.loads((shared / "checklists" / f"{t}.json").read_text()) for t in tasks}
    descriptions = {t: {c["id"]: c["description"] for c in checklists[t]["criteria"]} for t in tasks}
    incumbent = _trajectories(shared, tasks)
    tools = compact_declarations(tuple(d for p in sorted(profiles) for d in declarations_for_profile(p)))
    harness = loop.content(load_harness("v3")).as_dict()
    regret = loop._diagnostics(incumbent, None, None, [incumbent], tasks, descriptions, [])
    def size(payload):
        return len(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    proposal = size({"strategy": loop.SHARED_STRATEGY * 3, "incumbent": harness, "regret": regret, "tool_declarations": tools})
    worst = dict(regret)
    worst["contrasts"] = [{"task": t, "best_index": 0, "worst_index": 1, "strict_delta": 100.0, "continuous_delta": 100.0,
                           "better": incumbent[t][0].excerpt, "worse": incumbent[t][0].excerpt} for t in tasks]
    gate = {"promoted": False, "repair_eligible": True, "helped": list(tasks), "hurt": [tasks[0]], "net_gain": 1.0,
            "hurt_penalty": 1.0, "required_gain": 1.0, "reasons": ["hurt_budget"]}
    worst["rejections"] = [{"artifact": "0" * 64, "gate": gate, "rule": "x" * 800, "repair": None}] * rounds
    # Each promotion appends one bounded rule (<= 2500 chars) to a single field.
    big_harness = dict(harness, skills=harness["skills"] + ("\n\n[iff " + "c" * 240 + "] " + "r" * 2240) * rounds)
    worst_proposal = size({"strategy": "s" * 2600, "incumbent": big_harness, "regret": worst, "tool_declarations": tools})
    repair = size({"incumbent": big_harness, "candidate": big_harness, "gate": gate, "tool_declarations": tools,
                   "regret": {"winning_edit": {"field": "skills", "old": "", "new": "r" * 2500, "why": "w" * 1200},
                              "incumbent_regret": worst,
                              "hurt_replays": {tasks[0]: {"paired_delta": {"continuous": -50, "strict": -100, "criteria": []},
                                                          "incumbent": incumbent[tasks[0]][0].excerpt,
                                                          "winner": incumbent[tasks[0]][0].excerpt}}}})
    return {"tasks": len(tasks), "round1_proposal": proposal, "worst_proposal": worst_proposal, "worst_repair": repair,
            "limit": LIMIT, "fits": max(proposal, worst_proposal, repair) <= LIMIT}


def subset(manifest_families: dict, per_family: int):
    """Score-blind rule: first ``per_family`` cases of each family in manifest order."""
    taken, out = {}, []
    for case_id, family in manifest_families.items():
        if taken.get(family, 0) < per_family:
            taken[family] = taken.get(family, 0) + 1
            out.append(case_id)
    return tuple(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shared", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()
    index = json.loads(args.index.read_text())["splits"]
    order = lambda split: dict(sorted(index[split]["families"].items(), key=lambda kv: kv[0]))
    adaptation, gate = subset(order("adaptation"), 1), subset(order("gate"), 1)
    profiles = {"enterprise_ops:v1", "enterprise_ops:v2"}
    print(json.dumps({"adaptation": adaptation, "gate": gate,
                      "check": measure(args.shared, adaptation + gate, profiles),
                      "full_30_for_reference": measure(args.shared, tuple(order("adaptation")) + tuple(order("gate")), profiles)},
                     indent=1))


if __name__ == "__main__":
    main()
