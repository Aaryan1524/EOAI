"""EXPLORATORY (not pre-registered): dev-eval of the last promoted harness of an aborted arm.

The primary analysis scores aborted arms as the frozen harness (prereg §7). This script measures
what the aborted run had promoted before it stopped. Results are reported separately and never
enter the primary comparison.
"""

import argparse
import json
from pathlib import Path

from enterprise_ops.agent.harness import HarnessContent
from experiments.eoai_eval.execution import TrialConfig, fixed_conditions, verify_trial_artifacts
from experiments.eoai_eval.isolation import isolated_trial
from experiments.eoai_eval.scoring import score
from experiments.guard.__main__ import MODEL_SHA256, _read
from experiments.guard.loop import write_json

from .runner import _identity, _limits, core_digests, load_private


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, default=Path.home() / "EOAI-private" / "comparative-v1")
    parser.add_argument("--arm-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    core = core_digests()
    suite = load_private(args.private)["deveval"]
    source, runtime, model, baseline = _identity()
    decisions = sorted(args.arm_dir.glob("r*-decision.json"), key=lambda p: int(p.name[1:].split("-")[0]))
    last = _read(decisions[-1])["incumbent_after"]
    harness = HarnessContent(**_read(args.arm_dir / "harnesses" / f"{last}.json"))
    if harness.digest != last or last == baseline.digest:
        raise ValueError("No promoted harness to evaluate")
    args.out.mkdir(mode=0o700, parents=True)
    write_json(args.out / "protocol.json", {"phase": "exploratory-deveval", "arm_dir": str(args.arm_dir),
                                            "harness_digest": last, "guard_core_sha256": core,
                                            "suite_digest": suite.digest, "pre_registered": False})
    rows = []
    for case in suite.cases:
        config = TrialConfig(MODEL_SHA256, _limits(case.task), source, runtime)
        trial = isolated_trial(config, model, harness, case.snapshot, case.task, case.faults,
                               run_id=f"{last[:12]}-{case.id}-1", root=args.out, scenario=case.scenario)
        provenance = _read(trial.trace_path / "experiment.json")
        verify_trial_artifacts(trial.trace_path, trial.sandbox.path, provenance)
        if provenance["run"]["harness"]["digest"] != harness.digest:
            raise ValueError("Worker did not render the selected harness")
        report = score(case.snapshot.path, trial.sandbox.path, trial.trace_path, case.expectations)
        rows.append({"case_id": case.id, "replicate": 1, "passed": report["passed"], "checks": report.get("checks")})
    write_json(args.out / "evaluation.json", {"harness_digest": last, "results": rows, "pre_registered": False})
    print(json.dumps({"harness": last, "cases": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
