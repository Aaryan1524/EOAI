"""Run the pre-registered comparative experiment phases.

Phases (each a subcommand, each writing into a new directory):

    validate   load all three private splits and check disjointness (no model calls)
    calibrate  live reviewer calibration on the six known catalog fixtures (frozen v0 only)
    trial      one experimental trial: frozen checklists, a shared v0 batch, then the
               adaptive arms in a fixed order, each with its own role sampling seed
    deveval    independent oracle evaluation of each arm's final harness on the
               development-evaluation split (no reviewer, no adaptation component)

GUARD files under ``experiments/guard`` are imported, never edited. The only
behavioural substitution is the GUARD-without-veto ablation, which replaces
``loop.promotion_gate`` in-process for that arm alone.
"""

import argparse
import json
import os
import subprocess
import time
from dataclasses import asdict, replace
from hashlib import file_digest
from pathlib import Path

from enterprise_ops.agent.harness import HarnessContent, load_harness
from enterprise_ops.agent.model import OllamaModel, strict_json
from enterprise_ops.agent.runner import RunLimits
from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.sandbox import seed_snapshot
from enterprise_ops.tools.gateway import declarations_for_profile
from experiments.eoai_eval.datasets import load_adaptation, load_gate, load_heldout, validate_disjoint
from experiments.eoai_eval.execution import (
    ROOT, TrialConfig, fixed_conditions, runtime_identity, source_identity, verify_trial_artifacts,
)
from experiments.eoai_eval.isolation import isolated_trial
from experiments.eoai_eval.scoring import score
from experiments.guard import loop
from experiments.guard.__main__ import (
    MODEL_ID, MODEL_SHA256, _excerpt, _read, _safety_probe, frozen_manifest,
)
from experiments.guard.loop import Policy, Trajectory, paired_scores, run_rounds, write_json
from experiments.guard.math import GateDecision, GateReason, Verdict, soft_hurt_gate
from experiments.guard.review import derive_checklist, review_rollout
from experiments.guard.roles import RoleModel

from .naive import run_naive

GUARD_CORE = ("math.py", "review.py", "loop.py", "roles.py", "grounding.py", "semantic.py", "__main__.py",
              "audit.py")
PINNED_CORE_SHA256 = {
    "math.py": "a40ff61301e779b4af0996a3f87169eea29bc5094d79e4c9134a81381f7a2b16",
    "review.py": "2cfe59a026d917bd61655da02357be5d0c5520fbbf46b3e7d604e6f77480ac64",
    "loop.py": "9e80d33d27d9c3d393efd159e931042f1b01801b9b84f04b9709077904e13723",
    "roles.py": "70aa877da999fabc2a5663c14d56bbd9ee4697f247dd60873efaf7dd637a7ef2",
    "grounding.py": "3e5784d7a572503908fa288f5ea57b250e34a44fe36d56ef17907e2ee77b6164",
    "semantic.py": "63658ff74d1da389685bd8c6b03b678b6c64c8f3e5689c2d2d81f48f7c822cd9",
    "__main__.py": "4f023500b66586e515982bdc1408d523c25fbcc551e615b3d1d5f9b06701efeb",
    "audit.py": "9da938897d1a5a6c2cff0450a44ed3e2919ce75e81540aa838da5590b654b0a5",
}
ARMS = ("guard", "guard_noveto", "naive")
POLICY = Policy(pool_size=2, rounds=3, replicates=1)
DEVEVAL_REPLICATES = 1
DEADLINE_SECONDS = 180


def core_digests() -> dict:
    digests = {}
    for name in GUARD_CORE:
        with (ROOT / "experiments" / "guard" / name).open("rb") as stream:
            digests[name] = file_digest(stream, "sha256").hexdigest()
    for name, pinned in PINNED_CORE_SHA256.items():
        if digests[name] != pinned:
            raise ValueError(f"GUARD core file differs from the audited revision: {name}")
    return digests


def load_private(root: Path, splits=("adaptation", "gate", "deveval")) -> dict:
    suites = {split: load_heldout(root / split / "manifest.json", improvement_root=ROOT) for split in splits}
    validate_disjoint(load_adaptation(), load_gate(), *suites.values())
    return suites


def _identity():
    source = source_identity()
    baseline_manifest = frozen_manifest(source)
    model = OllamaModel(MODEL_ID)
    if model.model_digest() != MODEL_SHA256:
        raise ValueError("Local model digest differs from frozen baseline")
    runtime = runtime_identity(model)
    recorded = baseline_manifest["runtime"]
    if any(runtime[key] != recorded[key] for key in ("ollama", "python", "sqlite")):
        raise ValueError("Local runtime differs from frozen baseline")
    if model.settings != baseline_manifest["baseline"]["settings"]:
        raise ValueError("Model settings differ from frozen baseline")
    baseline = load_harness("v3")
    if baseline.digest != baseline_manifest["baseline"]["harness"]["sha256"]:
        raise ValueError("Packaged harness differs from frozen baseline")
    return source, runtime, model, baseline


def _limits(task) -> RunLimits:
    return RunLimits(tool_calls=12 if task.operation == "list_invoices" else 35, deadline_seconds=DEADLINE_SECONDS)


class Solver:
    """The GUARD entry point's rollout-and-review procedure, sourced from manifest cases."""

    def __init__(self, out, cases, checklists, roles, model, source, runtime, replicates, cache=None):
        self.out, self.cases, self.checklists, self.roles = out, cases, checklists, roles
        self.model, self.source, self.runtime, self.replicates = model, source, runtime, replicates
        self.conditions = {}
        self.cache = cache if cache is not None else {}
        (out / "reviews").mkdir(exist_ok=True)

    def __call__(self, harness, arm, tasks):
        result = {}
        for name in tasks:
            if arm == "v0" and name in self.cache:
                result[name] = self.cache[name]
                continue
            case = self.cases[name]
            config = TrialConfig(MODEL_SHA256, _limits(case.task), self.source, self.runtime)
            trials = []
            for replicate in range(1, self.replicates + 1):
                run_id = f"{arm}-{name}-{replicate}"
                trial = isolated_trial(config, self.model, harness, case.snapshot, case.task, case.faults,
                                       run_id=run_id, root=self.out, scenario=case.scenario)
                path = trial.trace_path
                provenance = _read(path / "experiment.json")
                verify_trial_artifacts(path, trial.sandbox.path, provenance)
                context = fixed_conditions(provenance)
                if context != self.conditions.setdefault(name, context):
                    raise ValueError(f"Paired run conditions changed for {name}")
                if provenance["run"]["harness"]["digest"] != harness.digest:
                    raise ValueError("Worker did not render the selected harness")
                summary = _read(path / "summary.json")
                review = review_rollout(self.checklists[name], trial, case.snapshot.path, self.roles, task=case.task)
                record = Trajectory(
                    name, path.name, tuple(Verdict(row.id, row.passed, row.why) for row in review.criteria),
                    review.blocking_error is not None,
                    summary | {"elapsed_seconds": provenance["elapsed_seconds"]},
                    _excerpt(path, _read(path / "final_output.json")),
                )
                write_json(self.out / "reviews" / f"{run_id}.json", record.record() | {
                    "review": review.as_dict(), "sandbox": str(trial.sandbox.path), "trace": str(path)})
                trials.append(record)
            result[name] = tuple(trials)
            if arm == "v0":
                self.cache[name] = result[name]
        return result


def no_veto_gate(candidate, incumbent, tasks, policy, *, repaired=False) -> GateDecision:
    """``loop.promotion_gate`` with the absolute required-action veto removed (ablation only)."""
    if not loop._reliable_pair(candidate, incumbent, tasks):
        return GateDecision(False, False, (), (), None, None, None, (GateReason.UNRELIABLE_REVIEW,))
    return soft_hurt_gate(
        paired_scores(candidate, tasks), paired_scores(incumbent, tasks),
        noise_floor=policy.noise_floor, max_hurt=policy.max_hurt, min_gain=policy.min_gain,
        repair_budget=policy.repair_budget, repaired=repaired,
    )


def trial_seeds(trial: int) -> dict:
    """Widely spaced role-sampling streams. GUARD and its no-veto ablation deliberately share one
    stream (common random numbers) so that the veto is the only systematic difference."""
    base = trial * 10**6
    return {"shared": base, "guard": base + 10**4, "guard_noveto": base + 10**4, "naive": base + 2 * 10**4}


def _strip(case):
    # Adaptation processes never hold oracle expectations.
    return replace(case, expectations={})


def _declarations(cases):
    return tuple(d for profile in sorted({c.profile for c in cases}) for d in declarations_for_profile(profile))


def _identities(cases):
    return tuple(i for c in cases for i in (c.task.account, c.task.customer_query, *(c.task.assigned_invoices or ())) if i)


def cmd_validate(args) -> dict:
    suites = load_private(args.private)
    out = {split: {"digest": suite.digest, "cases": [c.id for c in suite.cases]} for split, suite in suites.items()}
    out["guard_core_sha256"] = core_digests()
    print(json.dumps(out, indent=1))
    return out


def cmd_calibrate(args) -> None:
    core = core_digests()
    source, runtime, model, baseline = _identity()
    access = _safety_probe()
    roles = RoleModel(MODEL_SHA256, timeout=args.role_timeout, seed=0)
    out = args.out
    out.mkdir(mode=0o700, parents=True)
    names = ("acme-list", "acme-escalation", "meridian-conflict", "acme-account-escalation", "acme-paged-v2",
             "compound-escalation")
    entries = {n: load_task(n, authorize_writes=True) for n in names}

    class _Case:
        def __init__(self, entry, snapshot):
            self.task, self.faults, self.scenario, self.snapshot = entry.task, entry.faults, entry.scenario, snapshot

    cases = {n: _Case(e, seed_snapshot(out / "snapshots", n, profile=e.profile, scenario=e.scenario))
             for n, e in entries.items()}
    write_json(out / "protocol.json", {"phase": "calibration", "source": source, "runtime": runtime,
                                       "guard_core_sha256": core, "isolation": access, "tasks": names})
    try:
        checklists = {}
        (out / "checklists").mkdir()
        for n, e in entries.items():
            checklists[n] = derive_checklist(e.task.request, e.task.public_context(),
                                             declarations_for_profile(e.profile), roles)
            write_json(out / "checklists" / f"{n}.json", checklists[n].as_dict())
        Solver(out, cases, checklists, roles, model, source, runtime, args.replicates)(baseline, "v0", names)
    finally:
        write_json(out / "role-metrics.json", list(roles.metrics))


def cmd_trial(args) -> None:
    core = core_digests()
    suites = load_private(args.private, ("adaptation", "gate"))
    source, runtime, model, baseline = _identity()
    access = _safety_probe()
    out = args.out
    out.mkdir(mode=0o700, parents=True)
    cases = {c.id: _strip(c) for s in suites.values() for c in s.cases}
    training = tuple(c.id for c in suites["adaptation"].cases)
    replay = tuple(c.id for c in suites["gate"].cases)
    seeds = trial_seeds(args.trial)
    write_json(out / "protocol.json", {
        "phase": "trial", "trial": args.trial, "source": source, "runtime": runtime, "guard_core_sha256": core,
        "suite_digests": {k: v.digest for k, v in suites.items()}, "training": training, "replay": replay,
        "policy": asdict(POLICY), "arms": ARMS, "role_sampling_seeds": seeds, "isolation": access,
        "solver": {"model": MODEL_ID, "sha256": MODEL_SHA256, "settings": model.settings},
        "deadline_seconds": DEADLINE_SECONDS, "role_timeout_seconds": args.role_timeout,
    })
    # Checklists and the frozen-v0 batch are shared by all arms in the trial.
    shared_roles = RoleModel(MODEL_SHA256, timeout=args.role_timeout, seed=seeds["shared"])
    shared = out / "shared"
    shared.mkdir()
    (shared / "checklists").mkdir()
    checklists = {}
    try:
        for name in training + replay:
            c = cases[name]
            checklists[name] = derive_checklist(c.task.request, c.task.public_context(),
                                                declarations_for_profile(c.profile), shared_roles)
            write_json(shared / "checklists" / f"{name}.json", checklists[name].as_dict())
        started = time.time()
        base = Solver(shared, cases, checklists, shared_roles, model, source, runtime, POLICY.replicates)
        base(baseline, "v0", training + replay)
        write_json(shared / "timing.json", {"v0_seconds": time.time() - started})
    finally:
        write_json(shared / "role-metrics.json", list(shared_roles.metrics))
    descriptions = {n: {i.id: i.description for i in cl.criteria} for n, cl in checklists.items()}
    declarations = _declarations(cases.values())
    identities = _identities(cases.values())
    for arm in args.arms:
        arm_dir = out / arm
        arm_dir.mkdir()
        roles = RoleModel(MODEL_SHA256, timeout=args.role_timeout, seed=seeds[arm])
        solver = Solver(arm_dir, cases, checklists, roles, model, source, runtime, POLICY.replicates,
                        cache=dict(base.cache))
        solver.conditions = dict(base.conditions)
        started = time.time()
        status = {"arm": arm}
        original = loop.promotion_gate
        try:
            if arm == "naive":
                result = run_naive(arm_dir, baseline, training, {n: descriptions[n] for n in training}, solver,
                                   roles, POLICY, declarations=declarations, forbidden=identities)
            else:
                if arm == "guard_noveto":
                    loop.promotion_gate = no_veto_gate
                result = run_rounds(arm_dir, baseline, training, replay, descriptions, solver, roles, POLICY,
                                    declarations=declarations, forbidden=identities)
            status |= {"completed": True, "final": result["final"], "baseline": result["baseline"]}
        except Exception as error:  # noqa: BLE001 - every abort is recorded, never retried silently
            status |= {"completed": False, "error": f"{type(error).__name__}: {error}"}
        finally:
            loop.promotion_gate = original
            status["seconds"] = time.time() - started
            write_json(arm_dir / "role-metrics.json", list(roles.metrics))
            write_json(arm_dir / "status.json", status)


def cmd_deveval(args) -> None:
    core = core_digests()
    suites = load_private(args.private, ("adaptation", "gate", "deveval"))
    suite = suites["deveval"]
    source, runtime, model, baseline = _identity()
    trial_dir = args.trial_dir
    protocol = _read(trial_dir / "protocol.json")
    if protocol["guard_core_sha256"] != core:
        raise ValueError("GUARD core differs from the trial that produced the harnesses")
    for split in ("adaptation", "gate"):
        if protocol["suite_digests"][split] != suites[split].digest:
            raise ValueError("Trial suites differ from the current private splits")
    harnesses = {"frozen": baseline}
    arm_status = {}
    for arm in ARMS:
        path = trial_dir / arm / "status.json"
        status = _read(path) if path.exists() else {"completed": False, "error": "arm not run"}
        arm_status[arm] = status
        if not status.get("completed"):
            # Pre-registered: an aborted arm falls back to the frozen harness (intention to treat).
            harnesses[arm] = baseline
            continue
        final = status["final"]
        harnesses[arm] = baseline if final == status["baseline"] else HarnessContent(
            **_read(trial_dir / arm / "harnesses" / f"{final}.json"))
        if harnesses[arm].digest != final:
            raise ValueError(f"Stored harness for {arm} does not match its digest")
    out = args.out
    out.mkdir(mode=0o700, parents=True)
    digests = {arm: h.digest for arm, h in harnesses.items()}
    write_json(out / "protocol.json", {"phase": "deveval", "trial_dir": str(trial_dir), "source": source,
                                       "runtime": runtime, "guard_core_sha256": core, "suite_digest": suite.digest,
                                       "harness_digests": digests, "replicates": args.replicates,
                                       "arm_status": arm_status})
    # Identical harness texts are evaluated once and the result is shared (recorded explicitly).
    unique = {}
    for arm, harness in harnesses.items():
        unique.setdefault(harness.digest, (arm, harness))
    results = {}
    conditions = {}
    for digest, (label, harness) in unique.items():
        rows = []
        for index, case in enumerate(suite.cases, start=1):
            config = TrialConfig(MODEL_SHA256, _limits(case.task), source, runtime)
            for replicate in range(1, args.replicates + 1):
                run_id = f"{digest[:12]}-{case.id}-{replicate}"
                trial = isolated_trial(config, model, harness, case.snapshot, case.task, case.faults,
                                       run_id=run_id, root=out, scenario=case.scenario)
                provenance = _read(trial.trace_path / "experiment.json")
                verify_trial_artifacts(trial.trace_path, trial.sandbox.path, provenance)
                if provenance["run"]["harness"]["digest"] != harness.digest:
                    raise ValueError("Worker did not render the selected harness")
                context = fixed_conditions(provenance)
                if context != conditions.setdefault(case.id, context):
                    raise ValueError("Fixed conditions changed across arms")
                report = score(case.snapshot.path, trial.sandbox.path, trial.trace_path, case.expectations)
                write_json(trial.trace_path / "evaluation.json", report)
                rows.append({"case_id": case.id, "replicate": replicate, "passed": report["passed"],
                             "checks": report.get("checks"), "run_key": trial.trace_path.name,
                             "elapsed_seconds": provenance["elapsed_seconds"]})
        results[digest] = rows
        write_json(out / f"results-{digest[:12]}.json", rows)
    write_json(out / "evaluation.json", {"harness_digests": digests, "results": results, "arm_status": arm_status})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "calibrate", "trial", "deveval"):
        command = sub.add_parser(name)
        command.add_argument("--private", type=Path, default=Path.home() / "EOAI-private" / "comparative-v1")
        command.add_argument("--role-timeout", type=float, default=90)
        if name != "validate":
            command.add_argument("--out", type=Path, required=True)
        if name == "calibrate":
            command.add_argument("--replicates", type=int, default=1)
        if name == "trial":
            command.add_argument("--trial", type=int, required=True)
            command.add_argument("--arms", nargs="+", default=list(ARMS), choices=ARMS)
        if name == "deveval":
            command.add_argument("--trial-dir", type=Path, required=True)
            command.add_argument("--replicates", type=int, default=DEVEVAL_REPLICATES)
    args = parser.parse_args()
    if getattr(args, "out", None) is not None:
        args.out = args.out.resolve()
        if not args.out.is_relative_to(ROOT / "runs") or args.out.exists():
            parser.error("--out must be a new directory under runs")
    os.umask(0o077)
    {"validate": cmd_validate, "calibrate": cmd_calibrate, "trial": cmd_trial, "deveval": cmd_deveval}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
