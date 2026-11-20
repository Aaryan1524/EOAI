"""Trusted execution and provenance for harness comparisons."""

import json
import platform
import sqlite3
import subprocess
from dataclasses import dataclass
from hashlib import file_digest, sha256
from importlib import metadata
from pathlib import Path
from time import monotonic

import enterprise_ops
from enterprise_ops.agent.harness import Harness, HarnessContent, load_harness
from enterprise_ops.agent.model import OllamaModel, strict_json
from enterprise_ops.agent.runner import RunLimits, RunResult, RunTask, run_task
from enterprise_ops.sandbox import FaultPlan, Snapshot
from enterprise_ops.tools.gateway import declarations_for_profile


ROOT = Path(__file__).resolve().parents[2]


def sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def load_variant(path: Path | None) -> Harness | HarnessContent:
    if path is None:
        return load_harness("v3")
    with path.open("rb") as stream:
        raw = stream.read(400_001)
    if len(raw) > 400_000:
        raise ValueError("Harness bundle exceeds the size limit")
    content = strict_json(raw)
    if not isinstance(content, dict) or set(content) != {"instructions", "skills", "memory"}:
        raise ValueError("Harness bundle must contain only instructions, skills and memory")
    return HarnessContent(**content)


def _tree_digest(root: Path, paths: list[Path]) -> str:
    hashes = {path.relative_to(root).as_posix(): sha_file(path) for path in sorted(paths)}
    return sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def _files(directory: Path) -> list[Path]:
    return [
        path for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]


def source_identity(root: Path = ROOT) -> dict:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    source = root / "src" / "enterprise_ops"
    if Path(enterprise_ops.__file__).resolve().parent != source.resolve():
        raise ValueError("The imported EOAI package differs from the measured checkout")
    lockfile = root / "uv.lock"
    execution_files = _files(source) + [root / "pyproject.toml", lockfile]
    return {
        "commit": git("rev-parse", "HEAD"),
        "tracked_changes": bool(git("status", "--porcelain", "--untracked-files=no")),
        "execution_sha256": _tree_digest(root, execution_files),
        "tools_and_business_sha256": _tree_digest(root, [
            path for directory in ("tools", "domain", "storage") for path in _files(source / directory)
        ]),
        "evaluator_sha256": _tree_digest(root, _files(root / "experiments")),
        "dependency_lock_sha256": sha_file(lockfile),
    }


def runtime_identity(model: OllamaModel) -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "ollama": model.runtime_version(),
        "packages": dict(sorted(
            (item.metadata["Name"], item.version) for item in metadata.distributions()
        )),
    }


@dataclass(frozen=True)
class TrialConfig:
    model_digest: str
    limits: RunLimits
    source: dict
    runtime: dict


def run_trial(
    config: TrialConfig,
    model: OllamaModel,
    harness: Harness | HarnessContent,
    snapshot: Snapshot,
    task: RunTask,
    faults: FaultPlan,
    *,
    run_id: str,
    root: Path,
    scenario: str | None,
) -> RunResult:
    if sha_file(snapshot.path) != snapshot.digest:
        raise ValueError("Snapshot digest mismatch before trial")
    if model.model_digest() != config.model_digest:
        raise ValueError("Model digest mismatch before trial")
    started = monotonic()
    result = run_task(
        snapshot, task, model, run_id=run_id, runs_root=root, limits=config.limits, harness=harness,
        faults=faults.fresh(), model_digest=config.model_digest, scenario=scenario,
    )
    elapsed = monotonic() - started
    if sha_file(snapshot.path) != snapshot.digest:
        raise ValueError("Source snapshot changed during the trial")
    manifest = strict_json((result.trace_path / "manifest.json").read_bytes())
    (result.trace_path / "experiment.json").write_text(json.dumps({
        "schema_version": 1,
        "source": config.source,
        "runtime": config.runtime,
        "run": manifest,
        "tool_versions": {
            declaration["name"]: declaration["schema_version"]
            for declaration in declarations_for_profile(snapshot.profile)
        },
        "elapsed_seconds": elapsed,
        "artifacts": {
            **{path.name: sha_file(path) for path in sorted(result.trace_path.iterdir())},
            "run.sqlite3": sha_file(result.sandbox.path),
        },
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def fixed_conditions(provenance: dict) -> dict:
    """Return the fields that are invariant for repeated runs of one task."""
    run = provenance["run"]
    return {
        "source": provenance["source"],
        "runtime": provenance["runtime"],
        "tool_versions": provenance["tool_versions"],
        "run": {key: value for key, value in run.items() if key not in {"run_id", "sandbox_id", "harness"}},
    }


def verify_trial_artifacts(trace: Path, database: Path, provenance: dict) -> None:
    names = {"manifest.json", "input.json", "final_output.json", "summary.json",
             "state_delta.json", "evidence.json", "errors.json", "trace.jsonl"}
    actual = {name: sha_file(trace / name) for name in names if (trace / name).is_file()}
    actual["run.sqlite3"] = sha_file(database)
    if provenance["artifacts"] != actual:
        raise ValueError("Trial artifact digests differ from recorded digests")
    if provenance["run"] != strict_json((trace / "manifest.json").read_bytes()):
        raise ValueError("Trial manifest differs from recorded identity")
