"""Execute development trials or a sealed held-out evaluation."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from enterprise_ops.agent.model import ModelError, OllamaModel, strict_json
from enterprise_ops.agent.runner import RunLimits
from enterprise_ops.sandbox import seed_snapshot

from .datasets import load_adaptation, load_gate, load_heldout, validate_disjoint
from .execution import TrialConfig, fixed_conditions, load_variant, run_trial, runtime_identity, source_identity
from .scoring import score


def _write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _evaluate(args: argparse.Namespace) -> None:
    adaptation, gate = load_adaptation(), load_gate()
    if args.split == "heldout":
        suite = load_heldout(args.suite, improvement_root=args.improvement_root)
        if suite.digest != args.suite_digest:
            raise ValueError("Held-out suite digest differs from the locked digest")
        validate_disjoint(adaptation, gate, suite)
        if not args.out.is_absolute() or args.out.parent.resolve() != args.suite.resolve().parent:
            raise ValueError("Place held-out output directly inside the private suite directory")
    else:
        validate_disjoint(adaptation, gate)
        suite = adaptation if args.split == "adaptation" else gate
    cases = [case for case in suite.cases if args.case is None or case.id == args.case]
    if not cases:
        raise ValueError("No matching cases in the requested split")

    source = source_identity()
    if args.split == "heldout" and (
        source["tracked_changes"] or source["commit"] != args.commit
        or source["execution_sha256"] != args.execution_sha256
        or source["evaluator_sha256"] != args.evaluator_sha256
    ):
        raise ValueError("Evaluator or runner identity differs from the locked identity")
    if not 1 <= args.trials <= 100:
        raise ValueError("Run between one and 100 trials per case")

    harness = load_variant(args.harness)
    if args.split == "heldout" and harness.digest != args.harness_digest:
        raise ValueError("Harness digest differs from the preregistered digest")
    model = OllamaModel(args.model, args.endpoint)
    runtime = runtime_identity(model)
    args.out.mkdir(mode=0o700)
    reports = []
    for case_index, case in enumerate(cases, start=1):
        snapshot = case.snapshot
        if snapshot is None:
            snapshot = seed_snapshot(
                args.out / "snapshots", f"case-{case_index}", profile=case.profile, scenario=case.scenario,
            )
        limits = RunLimits(
            tool_calls=12 if case.task.operation == "list_invoices" else 35,
            deadline_seconds=args.deadline,
        )
        config = TrialConfig(args.model_digest, limits, source, runtime)
        condition = None
        trials = []
        for trial_number in range(1, args.trials + 1):
            result = run_trial(
                config, model, harness, snapshot, case.task, case.faults,
                run_id=f"case-{case_index}-trial-{trial_number}", root=args.out, scenario=case.scenario,
            )
            provenance = strict_json((result.trace_path / "experiment.json").read_bytes())
            current = fixed_conditions(provenance)
            if condition is not None and current != condition:
                raise ValueError("Fixed experiment conditions changed across trials")
            condition = current
            report = score(snapshot.path, result.sandbox.path, result.trace_path, case.expectations)
            _write(result.trace_path / "evaluation.json", report)
            trials.append({
                "run_key": result.trace_path.name,
                "passed": report["passed"],
                "semantic_outcome": report["semantic_outcome"],
            })
        reports.append({
            "case_id": case.id,
            "task_id": case.task.id,
            "semantic_outcomes_match": all(
                item["semantic_outcome"] == trials[0]["semantic_outcome"] for item in trials
            ),
            "trials": trials,
        })
    _write(args.out / "evaluation.json", {
        "split": args.split, "suite_digest": suite.digest, "cases": reports,
        "source": source, "runtime": runtime, "harness_digest": harness.digest,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="split", required=True)
    for split in ("adaptation", "gate", "heldout"):
        command = commands.add_parser(split)
        command.set_defaults(case=None)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--harness", type=Path)
        command.add_argument("--model", default="qwen3:4b-instruct")
        command.add_argument("--endpoint", default="http://127.0.0.1:11434")
        command.add_argument("--model-digest", required=True)
        command.add_argument("--deadline", type=float, default=180)
        command.add_argument("--trials", type=int, default=3)
        if split != "heldout":
            command.add_argument("--case")
        else:
            command.add_argument("--suite", type=Path, required=True)
            command.add_argument("--suite-digest", required=True)
            command.add_argument("--improvement-root", type=Path, required=True)
            command.add_argument("--commit", required=True)
            command.add_argument("--execution-sha256", required=True)
            command.add_argument("--evaluator-sha256", required=True)
            command.add_argument("--harness-digest", required=True)
    args = parser.parse_args()
    try:
        _evaluate(args)
    except (OSError, ValueError, ModelError, sqlite3.Error, subprocess.SubprocessError) as error:
        if args.split == "heldout":
            print("Held-out evaluation could not be completed. Contact the evaluator owner.", file=sys.stderr)
        else:
            print(f"Evaluation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
