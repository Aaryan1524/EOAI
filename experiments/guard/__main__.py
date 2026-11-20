"""Run GUARD on the fixed public EOAI development and replay buffer."""

import argparse
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from enterprise_ops.agent.harness import HarnessContent, load_harness
from enterprise_ops.agent.model import OllamaModel, strict_json
from enterprise_ops.agent.runner import RunLimits
from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.sandbox import seed_snapshot
from enterprise_ops.tools.gateway import declarations_for_profile
from experiments.eoai_eval.execution import (
    ROOT, TrialConfig, fixed_conditions, runtime_identity, source_identity, verify_trial_artifacts,
)
from experiments.eoai_eval.isolation import isolated_trial, verify_roles

from .loop import Policy, Trajectory, paired_scores, run_rounds, write_json
from .math import Verdict
from .review import derive_checklist, review_rollout
from .roles import RoleModel

BASELINE_COMMIT = "17f12c4fc10fead5448604dc14a08edea65b45a5"
BASELINE_TAG = "eoai-baseline-v0"
MODEL_ID = "qwen3:4b-instruct"
MODEL_SHA256 = "0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0"
TRAINING = ("acme-paged-v2", "acme-account-escalation")
REPLAY = ("compound-escalation", "acme-list", "meridian-conflict")


def frozen_manifest(source: dict) -> dict:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()

    if git("rev-parse", f"{BASELINE_TAG}^{{}}") != BASELINE_COMMIT:
        raise ValueError("Frozen baseline tag does not resolve to the pinned revision")
    if git("diff", "--name-only", BASELINE_COMMIT, "--", "src", "uv.lock", "pyproject.toml"):
        raise ValueError("Frozen solver source or dependencies differ from the baseline")
    baseline = strict_json(git("for-each-ref", "--format=%(contents)", f"refs/tags/{BASELINE_TAG}"))
    for key in ("execution_sha256", "tools_and_business_sha256", "dependency_lock_sha256"):
        if source[key] != baseline["source"][key]:
            raise ValueError(f"Frozen source digest mismatch: {key}")
    return baseline


def _read(path: Path):
    return strict_json(path.read_bytes())


def _excerpt(trace: Path, output: dict) -> dict:
    calls = []
    for line in (trace / "trace.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event.get("event") != "tool":
            continue
        response = event["response"]
        data = response.get("data")
        observed = {}
        if isinstance(data, dict):
            for key in ("decision", "status", "invoice_id", "reason_codes", "customer_id"):
                if key in data:
                    observed[key] = data[key]
            if "items" in data:
                observed["returned_ids"] = [row.get("id") for row in data["items"][:12]]
            if "next_cursor" in data:
                observed["has_next_page"] = data["next_cursor"] is not None
        calls.append({
            "tool": event["tool"], "args": event["arguments"], "ok": response["ok"],
            "error": response["error"]["code"] if response.get("error") else None,
            "observed": observed,
        })
    return {
        "calls": calls[:16], "status": output["status"], "coverage": output["coverage"],
        "missing": [row["code"] for row in output["missing_information"]],
    }


def _safety_probe() -> dict:
    # Disposable canaries only. Adaptation does not receive a held-out path.
    with TemporaryDirectory(prefix="eoai-guard-probe-", dir="/private/tmp") as work:
        workspace = Path(work)
        vault = workspace / "private"
        vault.mkdir(mode=0o700)
        answer = vault / "answer-canary"
        credential = workspace / "credential-canary"
        for path in (answer, credential):
            path.write_text("synthetic canary", encoding="utf-8")
        return verify_roles(vault, answer, credential)


def run(out: Path, policy: Policy, *, role_timeout: float = 90.0, sampling_seed: int = 0) -> dict:
    source = source_identity()
    baseline_manifest = frozen_manifest(source)
    model = OllamaModel(MODEL_ID)
    if model.model_digest() != MODEL_SHA256:
        raise ValueError("Local model digest differs from frozen baseline")
    runtime = runtime_identity(model)
    recorded_runtime = baseline_manifest["runtime"]
    if any(runtime[key] != recorded_runtime[key] for key in ("ollama", "python", "sqlite")):
        raise ValueError("Local runtime differs from frozen baseline")
    if model.settings != baseline_manifest["baseline"]["settings"]:
        raise ValueError("Model settings differ from frozen baseline")
    baseline = load_harness("v3")
    if baseline.digest != baseline_manifest["baseline"]["harness"]["sha256"]:
        raise ValueError("Packaged harness differs from frozen baseline")
    access = _safety_probe()
    roles = RoleModel(MODEL_SHA256, timeout=role_timeout, seed=sampling_seed)
    out.mkdir(mode=0o700, parents=True)
    (out / "reviews").mkdir()
    (out / "checklists").mkdir()
    write_json(out / "protocol.json", {
        "schema_version": 1, "frozen_commit": BASELINE_COMMIT, "source": source, "runtime": runtime,
        "solver": {"model": MODEL_ID, "sha256": MODEL_SHA256, "settings": model.settings},
        "frozen_harness_sha256": baseline.digest, "paper": "arXiv:2611.23476v1",
        "training": TRAINING, "replay": REPLAY, "policy": asdict(policy),
        "role_timeout_seconds": role_timeout, "proposal_sampling_seed": sampling_seed, "isolation": access,
    })
    entries = {task: load_task(task, authorize_writes=True) for task in TRAINING + REPLAY}
    snapshots = {
        name: seed_snapshot(out / "snapshots", name, profile=entry.profile, scenario=entry.scenario)
        for name, entry in entries.items()
    }
    conditions = {}
    checklists = {}
    try:
        # All checklists are frozen before any solver rollout.
        for name, entry in entries.items():
            task = entry.task
            checklist = derive_checklist(
                task.request, task.public_context(), declarations_for_profile(entry.profile), roles,
            )
            checklists[name] = checklist
            write_json(out / "checklists" / f"{name}.json", checklist.as_dict())

        def solve(harness, arm, tasks):
            result = {}
            for name in tasks:
                entry = entries[name]
                limits = RunLimits(tool_calls=12 if entry.task.operation == "list_invoices" else 35,
                                   deadline_seconds=180)
                config = TrialConfig(MODEL_SHA256, limits, source, runtime)
                trials = []
                for replicate in range(1, policy.replicates + 1):
                    run_id = f"{arm}-{name}-{replicate}"
                    trial = isolated_trial(
                        config, model, harness, snapshots[name], entry.task, entry.faults,
                        run_id=run_id, root=out, scenario=entry.scenario,
                    )
                    path = trial.trace_path
                    provenance = _read(path / "experiment.json")
                    verify_trial_artifacts(path, trial.sandbox.path, provenance)
                    context = fixed_conditions(provenance)
                    if context != conditions.setdefault(name, context):
                        raise ValueError(f"Paired run conditions changed for {name}")
                    if provenance["run"]["harness"]["digest"] != harness.digest:
                        raise ValueError("Worker did not render the selected harness")
                    summary = _read(path / "summary.json")
                    review = review_rollout(checklists[name], trial, snapshots[name].path, roles, task=entry.task)
                    record = Trajectory(
                        name, path.name, tuple(Verdict(row.id, row.passed, row.why) for row in review.criteria),
                        review.blocking_error is not None,
                        summary | {"elapsed_seconds": provenance["elapsed_seconds"]},
                        _excerpt(path, _read(path / "final_output.json")),
                    )
                    write_json(out / "reviews" / f"{run_id}.json",
                               record.record() | {"review": review.as_dict()})
                    trials.append(record)
                result[name] = tuple(trials)
            return result

        descriptions = {name: {item.id: item.description for item in checklist.criteria}
                        for name, checklist in checklists.items()}
        declarations = tuple(
            declaration for profile in sorted({entry.profile for entry in entries.values()})
            for declaration in declarations_for_profile(profile)
        )
        identities = tuple(
            identity for entry in entries.values()
            for identity in (entry.task.account, entry.task.customer_query, *(entry.task.assigned_invoices or ()))
            if identity
        )
        result = run_rounds(out, baseline, TRAINING, REPLAY, descriptions, solve, roles, policy,
                            declarations=declarations, forbidden=identities)
        # Independent repeat after the decision. It cannot feed back into optimization.
        final_path = out / "harnesses" / f"{result['final']}.json"
        final = baseline if result["final"] == result["baseline"] else HarnessContent(**_read(final_path))
        verification = solve(final, "post", TRAINING + REPLAY)
        write_json(out / "post-selection.json", {
            "artifact": result["final"],
            "scores": {name: asdict(score) for name, score in paired_scores(verification, TRAINING+REPLAY).items()},
        })
        return result
    finally:
        write_json(out / "role-metrics.json", roles.metrics)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--pool-size", type=int, default=2)
    parser.add_argument("--replicates", type=int, default=1)
    parser.add_argument("--role-timeout", type=float, default=90)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    out = args.out.resolve()
    if not out.is_relative_to(ROOT / "runs") or out == ROOT / "runs":
        parser.error("--out must be a new directory under runs")
    if out.exists():
        parser.error("--out must not exist")
    os.umask(0o077)
    try:
        answer = run(out, Policy(pool_size=args.pool_size, rounds=args.rounds, replicates=args.replicates),
                     role_timeout=args.role_timeout, sampling_seed=args.seed)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"GUARD experiment failed: {error}\n")
    print(f"Decision: {answer['rounds'][-1]['decision']}. Evidence: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
