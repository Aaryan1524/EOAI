"""Post-selection independent scoring for the public phase-one pilot."""

import argparse
import json
from pathlib import Path

from enterprise_ops.agent.model import strict_json
from experiments.eoai_eval.datasets import load_adaptation, load_gate
from experiments.eoai_eval.execution import ROOT, sha_file, verify_trial_artifacts
from experiments.eoai_eval.scoring import score

from .loop import write_json


def audit(path: Path) -> dict:
    selection = path / "selection.json"
    post = path / "post-selection.json"
    if not selection.is_file() or not post.is_file():
        raise ValueError("The optimization and post-selection run must finish before independent scoring")
    cases = {case.id: case for suite in (load_adaptation(), load_gate()) for case in suite.cases}
    entries = {}
    for review_path in sorted((path / "reviews").glob("*.json")):
        review = strict_json(review_path.read_bytes())
        name, key = review["task_id"], review["trace_key"]
        trace = path / "traces" / key
        database = path / "sandboxes" / key / "run.sqlite3"
        verify_trial_artifacts(trace, database, strict_json((trace / "experiment.json").read_bytes()))
        result = score(path / "snapshots" / f"{name}.sqlite3", database, trace, cases[name].expectations)
        entries[review_path.stem] = {
            "task_id": name, "trace_key": key, "passed": result["passed"],
            "checks": result["checks"], "details": result["details"],
        }
    report = {"selection_sha256": sha_file(selection), "post_selection_sha256": sha_file(post),
              "trials": entries}
    write_json(path / "independent-development-audit.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    path = args.run.resolve()
    if not path.is_relative_to(ROOT / "runs") or path == ROOT / "runs":
        parser.error("Choose a completed development run under runs")
    result = audit(path)
    print(json.dumps({name: entry["passed"] for name, entry in result["trials"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
