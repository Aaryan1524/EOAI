"""Operator commands for isolated, reproducible local sandbox runs."""

import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import fields
from datetime import datetime
from hashlib import file_digest
from pathlib import Path
from tempfile import TemporaryDirectory

from enterprise_ops.agent import runner
from enterprise_ops.agent.harness import Harness, load_harness
from enterprise_ops.agent.model import ModelError, OllamaModel, strict_json
from enterprise_ops.agent.trace import read_state_delta, scrub
from enterprise_ops.clock import FrozenClock
from enterprise_ops.fixtures.catalog import CatalogTask, load_task, task_names
from enterprise_ops.sandbox import Snapshot, seed_snapshot
from enterprise_ops.storage.audit import stamp
from enterprise_ops.storage.sqlite import connect, transaction
from enterprise_ops.tools.session import digest


_DEFAULT_MODEL = "qwen3:4b-instruct"
_DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
_COMPONENT_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_COMPONENT = re.compile(_COMPONENT_PATTERN)
_RUN_KEY = re.compile(rf"({_COMPONENT_PATTERN})-(sandbox-[0-9a-f]{{32}})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_FIELDS = ("id", "profile", "clock", "scenario")


def _component(value: str, label: str) -> str:
    if _COMPONENT.fullmatch(value) is None or scrub(value) != value:
        raise ValueError(f"{label} must be a safe path component")
    return value


def _read_json(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing or unsafe artifact {path.name}")
    return strict_json(path.read_bytes())


def _object(path: Path) -> dict:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"Invalid object in {path.name}")
    return value


def _directory(path: Path, *, create: bool = False) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"Unsafe directory {path.name}")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _snapshot_path(root: Path, snapshot_id: str) -> Path:
    return _directory(_directory(root / "snapshots") / _component(snapshot_id, "Snapshot ID"))


def _seed(root: Path, snapshot_id: str, entry: CatalogTask) -> dict:
    target = _snapshot_path(root, snapshot_id)
    if target.exists():
        raise ValueError(f"Snapshot already exists: {snapshot_id}")
    parent = _directory(target.parent, create=True)
    with TemporaryDirectory(prefix=".seed-", dir=parent) as work:
        staged = Path(work) / "publish"
        snapshot = seed_snapshot(staged, snapshot_id, profile=entry.profile, scenario=entry.scenario)
        database = staged / "snapshot.sqlite3"
        snapshot.path.rename(database)
        values = (snapshot_id, entry.profile, stamp(snapshot.clock.instant), entry.scenario)
        with closing(connect(database)) as db, transaction(db, write=True):
            db.execute(
                "CREATE TABLE cli_snapshot (profile TEXT NOT NULL, clock TEXT NOT NULL, scenario TEXT NOT NULL)"
            )
            db.execute("INSERT INTO cli_snapshot VALUES (?, ?, ?)", values[1:])
        metadata = dict(zip(_SNAPSHOT_FIELDS, values, strict=True), digest=_sha256(database))
        (staged / "snapshot.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        staged.rename(target)
    return metadata


def _load_snapshot(root: Path, snapshot_id: str) -> tuple[Snapshot, str]:
    directory = _snapshot_path(root, snapshot_id)
    if not directory.is_dir():
        raise ValueError(f"Unknown snapshot: {snapshot_id}")
    metadata = _object(directory / "snapshot.json")
    if (
        metadata.keys() != {*_SNAPSHOT_FIELDS, "digest"}
        or any(not isinstance(value, str) or not value for value in metadata.values())
        or metadata["id"] != snapshot_id
        or _SHA256.fullmatch(metadata["digest"]) is None
    ):
        raise ValueError("Invalid snapshot metadata")
    database = directory / "snapshot.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise ValueError("Missing or unsafe snapshot database")
    if _sha256(database) != metadata["digest"]:
        raise ValueError("Snapshot digest mismatch")
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
        records = db.execute("SELECT profile, clock, scenario FROM cli_snapshot LIMIT 2").fetchall()
    if records != [tuple(metadata[field] for field in _SNAPSHOT_FIELDS[1:])]:
        raise ValueError("Snapshot metadata does not match the database")
    clock = FrozenClock(datetime.fromisoformat(metadata["clock"]))
    return Snapshot(snapshot_id, database, metadata["digest"], metadata["profile"], clock), metadata["scenario"]


def _trace(root: Path, run_key: str) -> Path:
    traces = _directory(root / "traces")
    key = _RUN_KEY.fullmatch(run_key)
    if key and scrub(key[1]) == key[1]:
        exact = _directory(traces / run_key)
        if exact.is_dir():
            return exact
    _component(run_key, "Run key or run ID")
    matches = []
    if traces.is_dir():
        for path in traces.iterdir():
            match = _RUN_KEY.fullmatch(path.name)
            if match and match[1] == run_key and _directory(path).is_dir():
                matches.append(path)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError(f"Ambiguous run ID {run_key!r}. Use the full run key.")
    raise ValueError(f"Unknown run: {run_key}")


def _manifest(trace: Path) -> dict:
    manifest = _object(trace / "manifest.json")
    identity = _RUN_KEY.fullmatch(trace.name)
    if manifest.get("run_id") != identity[1] or manifest.get("sandbox_id") != identity[2]:
        raise ValueError("Run manifest identity does not match the trace directory")
    return manifest


def _inspect(root: Path, run_key: str) -> dict:
    trace = _trace(root, run_key)
    manifest = _manifest(trace)
    final, status, state = (trace / name for name in ("final_output.json", "summary.json", "state_delta.json"))
    output = _object(final) if final.exists() else None
    summary = _object(status) if status.exists() else {"status": "interrupted"}
    if output is not None and state.exists():
        audit = _read_json(state)
        if not isinstance(audit, list):
            raise ValueError("Invalid audit state delta")
    else:
        database = _directory(_directory(root / "sandboxes") / trace.name) / "run.sqlite3"
        if database.is_symlink() or not database.is_file():
            raise ValueError("Interrupted run has no accessible sandbox database")
        tenant_id = _captured_text(_object(trace / "input.json"), "tenant_id")
        audit = read_state_delta(database, tenant_id, manifest["run_id"])
    return {
        "run_key": trace.name, "trace_path": str(trace), "manifest": manifest,
        "output": output, "summary": summary, "audit": scrub(audit),
    }


def _task_for_snapshot(
    task_name: str, snapshot: Snapshot, scenario: str, authorize_writes: bool,
) -> CatalogTask:
    entry = load_task(task_name, authorize_writes=authorize_writes)
    if entry.profile != snapshot.profile or entry.scenario != scenario:
        raise ValueError("Task fixture scenario or tool profile does not match the snapshot")
    return entry


def _model_id(value: str) -> str:
    if (
        not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.strip()
        or value != value.strip() or scrub(value) != value
    ):
        raise ValueError("Invalid model ID")
    return value


def _model_digest(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value.removeprefix("sha256:")) is None:
        raise ValueError("Invalid model digest")
    return value.removeprefix("sha256:")


def _run(
    root: Path, snapshot: Snapshot, entry: CatalogTask, model: OllamaModel,
    model_digest: str, run_id: str, harness: Harness, limits: runner.RunLimits | None,
) -> dict:
    _directory(root / "sandboxes")
    _directory(root / "traces")
    result = runner.run_task(
        snapshot, entry.task, model, run_id=run_id, runs_root=root, harness=harness,
        limits=limits, faults=entry.faults, model_digest=model_digest, scenario=entry.scenario,
    )
    return {
        "run_key": result.trace_path.name,
        "trace_path": str(result.trace_path),
        "output": scrub(result.output.model_dump(mode="json")),
    }


def _new_run(root: Path, args: argparse.Namespace) -> dict:
    run_id = _component(args.id, "Run ID")
    snapshot, scenario = _load_snapshot(root, args.snapshot_id)
    entry = _task_for_snapshot(args.task, snapshot, scenario, args.authorize_writes)
    harness = load_harness(args.harness)
    limits = None
    if args.deadline is not None:
        limits = runner.RunLimits(
            tool_calls=12 if entry.task.operation == "list_invoices" else 35,
            deadline_seconds=args.deadline,
        )
    model = OllamaModel(_model_id(args.model), args.endpoint)
    model_digest = _model_digest(model.model_digest())
    return _run(root, snapshot, entry, model, model_digest, run_id, harness, limits)


def _captured_text(manifest: dict, field: str) -> str:
    value = manifest.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Run manifest is missing {field}")
    return value


def _captured_limits(manifest: dict) -> runner.RunLimits:
    limits = manifest.get("limits")
    if (
        not isinstance(limits, dict) or limits.keys() != {field.name for field in fields(runner.RunLimits)}
        or type(limits["deadline_seconds"]) not in (int, float)
    ):
        raise ValueError("Invalid captured run limits")
    return runner.RunLimits(**limits)


def _verify_replay_context(
    trace: Path, manifest: dict, snapshot: Snapshot, entry: CatalogTask,
) -> None:
    from enterprise_ops.tools.gateway import declarations_for_profile

    task = entry.task
    expected = {
        "snapshot_digest": snapshot.digest,
        "profile": snapshot.profile,
        "scenario": entry.scenario,
        "as_of": stamp(snapshot.clock.instant),
        "task_digest": runner.task_digest(task),
        "fault_plan_digest": runner.fault_plan_digest(entry.faults),
        "tool_declarations_digest": digest(declarations_for_profile(snapshot.profile)),
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise ValueError(f"Replay context mismatch for {field}")
    captured_harness = manifest.get("harness")
    if (
        not isinstance(captured_harness, dict) or not isinstance(captured_harness.get("version"), str)
        or not isinstance(captured_harness.get("digest"), str)
        or _SHA256.fullmatch(captured_harness["digest"]) is None
    ):
        raise ValueError("Invalid captured harness identity")
    original_harness = load_harness(captured_harness["version"])
    if original_harness.digest != captured_harness["digest"]:
        raise ValueError("Replay context mismatch for harness digest")
    original_input = _object(trace / "input.json")
    expected_input = scrub({
        "task": task.public_context(), "request": task.request,
        "tenant_id": task.principal.tenant_id, "user_id": task.principal.user_id,
    })
    if any(original_input.get(field) != value for field, value in expected_input.items()):
        raise ValueError("Replay context mismatch for captured task input")
    if original_input.get("rendered_instructions") != scrub(original_harness.instructions):
        raise ValueError("Replay context mismatch for captured harness instructions")


def _replay(root: Path, args: argparse.Namespace) -> dict:
    run_id = _component(args.id, "Run ID")
    trace = _trace(root, args.run_key)
    manifest = _manifest(trace)
    if type(manifest.get("manifest_version")) is not int or manifest["manifest_version"] != 1:
        raise ValueError("Run manifest version is unsupported for replay")
    if run_id == manifest["run_id"]:
        raise ValueError("Replay requires a new run ID")
    snapshot, scenario = _load_snapshot(root, _captured_text(manifest, "snapshot_id"))
    entry = _task_for_snapshot(
        _captured_text(manifest, "task_id"), snapshot, scenario, args.authorize_writes,
    )
    if "write_scope_digest" not in manifest:
        raise ValueError("Run manifest is missing write_scope_digest")
    captured_scope = manifest["write_scope_digest"]
    if captured_scope is not None and not args.authorize_writes:
        raise ValueError("Replay of the captured write scope requires --authorize-writes")
    scope = entry.task.write_scope
    if captured_scope != (scope.digest if scope else None):
        raise ValueError("Replay context mismatch for write_scope_digest")
    captured_harness = manifest.get("harness")
    if not isinstance(captured_harness, dict):
        raise ValueError("Invalid captured harness identity")
    version = _captured_text(captured_harness, "version")
    harness = load_harness(args.harness if args.harness is not None else version)
    _verify_replay_context(trace, manifest, snapshot, entry)
    limits = _captured_limits(manifest)
    model = OllamaModel(_model_id(_captured_text(manifest, "model_id")), args.endpoint)
    if (
        manifest.get("provider") != model.provider
        or json.dumps(manifest.get("model_settings"), sort_keys=True) != json.dumps(model.settings, sort_keys=True)
    ):
        raise ValueError("Replay context mismatch for model provider or settings")
    captured_digest = _model_digest(_captured_text(manifest, "model_digest"))
    model_digest = _model_digest(model.model_digest())
    if captured_digest != model_digest:
        raise ValueError("Replay context mismatch for model digest")
    return _run(root, snapshot, entry, model, model_digest, run_id, harness, limits)


def _reset(root: Path, args: argparse.Namespace) -> dict:
    run_id = _component(args.id, "Run ID")
    snapshot, scenario = _load_snapshot(root, args.snapshot_id)
    sandbox = snapshot.clone(_directory(root / "sandboxes"), run_id)
    return {
        "run_key": sandbox.path.parent.name, "sandbox_path": str(sandbox.path),
        "snapshot_id": snapshot.id, "snapshot_digest": snapshot.digest,
        "profile": snapshot.profile, "scenario": scenario, "as_of": stamp(snapshot.clock.instant),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ops-agent", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("runs"), help="Artifact directory (default: runs)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tasks", help="List built-in task assignments")
    seed = commands.add_parser("seed", help="Publish a named fixture snapshot")
    seed.add_argument("snapshot_id")
    seed.add_argument("--task", default="acme-list", choices=task_names())
    run = commands.add_parser("run", help="Run a built-in task in a fresh sandbox")
    run.add_argument("snapshot_id")
    run.add_argument("--task", required=True, choices=task_names())
    run.add_argument("--id", required=True)
    run.add_argument("--model", default=_DEFAULT_MODEL)
    run.add_argument("--harness", default="v1")
    run.add_argument("--deadline", type=float, metavar="SECONDS")
    inspect = commands.add_parser("inspect", help="Show a run's output, summary, and audit delta")
    inspect.add_argument("run_key")
    replay = commands.add_parser("replay", help="Run a recorded task in a fresh sandbox")
    replay.add_argument("run_key")
    replay.add_argument("--id", required=True)
    replay.add_argument("--harness")
    reset = commands.add_parser("reset", help="Clone a fresh sandbox and preserve existing runs")
    reset.add_argument("snapshot_id")
    reset.add_argument("--id", required=True)
    for command in (run, replay):
        command.add_argument("--authorize-writes", action="store_true")
        command.add_argument("--endpoint", default=_DEFAULT_ENDPOINT, metavar="LOOPBACK")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Print one JSON result on success and return a command exit code."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return error.code

    try:
        root = args.root.resolve()
        match args.command:
            case "tasks":
                result = [
                    {"name": entry.task.id, "scenario": entry.scenario, "profile": entry.profile,
                     "level": entry.level, "request": entry.task.request}
                    for entry in map(load_task, task_names())
                ]
            case "seed":
                result = _seed(root, args.snapshot_id, load_task(args.task))
            case "run":
                result = _new_run(root, args)
            case "inspect":
                result = _inspect(root, args.run_key)
            case "replay":
                result = _replay(root, args)
            case "reset":
                result = _reset(root, args)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, OSError, sqlite3.Error, ModelError, RecursionError) as error:
        print(f"ops-agent: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
