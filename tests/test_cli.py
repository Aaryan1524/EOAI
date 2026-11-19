import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from enterprise_ops import cli
from enterprise_ops.agent import runner
from enterprise_ops.agent.harness import Harness, load_harness
from enterprise_ops.agent.trace import RunTrace
from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import Principal
from enterprise_ops.storage.repository import transition_payment
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools import gateway
from enterprise_ops.tools.session import digest


def _invoke(root, capsys, *args, status=0):
    assert cli.main(["--root", str(root), *args]) == status
    capture = capsys.readouterr()
    if status:
        assert not capture.out
        return capture.err
    assert not capture.err
    return json.loads(capture.out)


def _alter(path: Path, **changes):
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def local_provider(monkeypatch):
    calls = []
    identity_calls = []
    registered_digest = "a" * 64
    settings = {"context_window": 32768, "temperature": 0.0, "seed": 0}

    class FakeOllama:
        provider = "ollama"

        def __init__(self, model_id, endpoint):
            self.model_id = model_id
            self.endpoint = endpoint
            self.settings = settings

        def model_digest(self):
            identity_calls.append(self.endpoint)
            return registered_digest

    def task_digest(task):
        return digest([task.id, task.request])

    def fault_plan_digest(faults):
        return digest([asdict(step) for step in faults._steps])

    def declarations_for_profile(profile):
        return [{"profile": profile}]

    def run_task(snapshot, task, model, *, run_id, runs_root, harness, limits, faults, model_digest, scenario):
        calls.append((model, run_id, task, harness, limits, faults))
        sandbox = snapshot.clone(runs_root / "sandboxes", run_id)
        trace = RunTrace(runs_root / "traces", sandbox)
        trace.write("manifest", {
            "manifest_version": 1, "run_id": run_id, "sandbox_id": sandbox.id, "snapshot_id": snapshot.id,
            "snapshot_digest": snapshot.digest, "profile": snapshot.profile, "scenario": scenario,
            "as_of": trace.as_of, "task_id": task.id, "task_digest": task_digest(task),
            "fault_plan_digest": fault_plan_digest(faults),
            "write_scope_digest": task.write_scope.digest if task.write_scope else None,
            "tool_declarations_digest": digest(declarations_for_profile(snapshot.profile)),
            "provider": model.provider, "model_id": model.model_id, "model_digest": model_digest,
            "model_settings": model.settings,
            "harness": {"version": harness.version, "digest": harness.digest},
            "limits": asdict(limits or runner.RunLimits()),
        })
        trace.write("input", {
            "task": task.public_context(), "request": task.request, "tenant_id": task.principal.tenant_id,
            "user_id": task.principal.user_id, "rendered_instructions": harness.instructions,
        })
        output = {"status": "completed"}
        trace.write("final_output", output)
        trace.write("summary", output)
        trace.write("state_delta", [{"action": "ticket.create", "id": "audit_test"}] if task.write_scope else [])
        return SimpleNamespace(
            output=SimpleNamespace(model_dump=lambda *, mode: output), trace_path=trace.path, sandbox=sandbox,
        )

    monkeypatch.setattr(cli, "OllamaModel", FakeOllama)
    monkeypatch.setattr(runner, "run_task", run_task)
    monkeypatch.setattr(runner, "task_digest", task_digest, raising=False)
    monkeypatch.setattr(runner, "fault_plan_digest", fault_plan_digest, raising=False)
    monkeypatch.setattr(gateway, "declarations_for_profile", declarations_for_profile, raising=False)
    return SimpleNamespace(calls=calls, identity_calls=identity_calls)


def test_catalog_seed_run_inspect_and_reset(tmp_path, capsys, local_provider):
    catalog = _invoke(tmp_path, capsys, "tasks")
    assert [entry["name"] for entry in catalog] == [
        "acme-list", "acme-escalation", "meridian-conflict", "acme-paged-v2", "compound-escalation",
    ]

    metadata = _invoke(tmp_path, capsys, "seed", "base")
    assert metadata["scenario"] == "baseline"
    assert metadata["profile"] == "enterprise_ops:v1"
    original = _invoke(
        tmp_path, capsys, "run", "base", "--task", "acme-escalation", "--id", "first",
        "--deadline", "45", "--authorize-writes",
    )
    assert local_provider.calls[0][2].write_scope is not None
    assert local_provider.calls[0][4].deadline_seconds == 45
    assert local_provider.calls[0][0].model_id == "qwen3:4b-instruct"

    inspected = _invoke(tmp_path, capsys, "inspect", "first")
    assert inspected == _invoke(tmp_path, capsys, "inspect", original["run_key"])
    assert inspected["output"] == inspected["summary"] == original["output"]
    assert inspected["audit"] == [{"action": "ticket.create", "id": "audit_test"}]

    fresh = _invoke(tmp_path, capsys, "reset", "base", "--id", "restored")
    assert sha256(Path(fresh["sandbox_path"]).read_bytes()).hexdigest() == metadata["digest"]
    assert fresh["run_key"] != original["run_key"]
    assert _invoke(tmp_path, capsys, "inspect", original["run_key"]) == inspected


def test_replay_requires_matching_explicit_write_scope(tmp_path, capsys, local_provider):
    _invoke(tmp_path, capsys, "seed", "base")
    original = _invoke(
        tmp_path, capsys, "run", "base", "--task", "acme-escalation",
        "--id", "first", "--deadline", "45", "--authorize-writes",
    )
    error = _invoke(tmp_path, capsys, "replay", original["run_key"], "--id", "second", status=1)
    assert "--authorize-writes" in error
    assert len(local_provider.identity_calls) == 1

    repeated = _invoke(
        tmp_path, capsys, "replay", original["run_key"], "--id", "second",
        "--authorize-writes", "--endpoint", "http://localhost:14141",
    )
    assert repeated["run_key"] != original["run_key"]
    assert local_provider.calls[0][2:5] == local_provider.calls[1][2:5]
    assert local_provider.identity_calls[-1] == "http://localhost:14141"

    read_only = _invoke(tmp_path, capsys, "run", "base", "--task", "acme-escalation", "--id", "read")
    error = _invoke(
        tmp_path, capsys, "replay", read_only["run_key"], "--id", "third", "--authorize-writes", status=1,
    )
    assert "write_scope_digest" in error


@pytest.mark.parametrize("field,value", [
    ("snapshot_digest", "b" * 64),
    ("profile", "enterprise_ops:v2"),
    ("scenario", "compound-escalation"),
    ("as_of", "2028-01-01"),
    ("task_digest", "b" * 64),
    ("fault_plan_digest", "b" * 64),
    ("tool_declarations_digest", "b" * 64),
    ("provider", "remote"),
    ("model_settings", {"temperature": 0.2}),
    ("model_digest", "b" * 64),
    ("limits", {"deadline_seconds": True}),
    ("harness", {"version": "v1", "digest": "b" * 64}),
])
def test_replay_rejects_context_drift(tmp_path, capsys, local_provider, field, value):
    _invoke(tmp_path, capsys, "seed", "base")
    original = _invoke(tmp_path, capsys, "run", "base", "--task", "acme-list", "--id", "first")
    path = Path(original["trace_path"])
    _alter(path / "manifest.json", **{field: value})
    error = _invoke(tmp_path, capsys, "replay", original["run_key"], "--id", "second", status=1)
    assert field.split("_")[0] in error
    assert len(local_provider.calls) == 1


def test_replay_checks_input_and_accepts_an_explicit_alternate_harness(tmp_path, capsys, local_provider, monkeypatch):
    _invoke(tmp_path, capsys, "seed", "base")
    original = _invoke(tmp_path, capsys, "run", "base", "--task", "acme-list", "--id", "first")
    content = "alternate instructions"
    alternate = Harness("v2", content, sha256(content.encode()).hexdigest())
    monkeypatch.setattr(cli, "load_harness", lambda version: alternate if version == "v2" else load_harness(version))
    _invoke(tmp_path, capsys, "replay", "first", "--id", "second", "--harness", "v2")
    assert local_provider.calls[-1][3] == alternate
    _alter(Path(original["trace_path"]) / "input.json", request="different request")
    error = _invoke(tmp_path, capsys, "replay", "first", "--id", "third", "--harness", "v2", status=1)
    assert "captured task input" in error


def test_replay_still_checks_original_harness_with_override(tmp_path, capsys, local_provider):
    _invoke(tmp_path, capsys, "seed", "base")
    original = _invoke(tmp_path, capsys, "run", "base", "--task", "acme-list", "--id", "first")
    _alter(Path(original["trace_path"]) / "manifest.json", harness={"version": "v1", "digest": "b" * 64})
    message = _invoke(tmp_path, capsys, "replay", "first", "--id", "second", "--harness", "v2", status=1)
    assert "harness digest" in message


def test_inspect_reads_committed_audit_from_an_interrupted_run(tmp_path, capsys, local_provider):
    seeded = _invoke(tmp_path, capsys, "seed", "base")
    original = _invoke(tmp_path, capsys, "run", "base", "--task", "acme-list", "--id", "first")
    trace = Path(original["trace_path"])
    sandbox = tmp_path / "sandboxes" / original["run_key"] / "run.sqlite3"
    with closing(connect(sandbox)) as db:
        transition_payment(
            db, Principal("northstar_property", "simulator"), "PMT-2", "posted",
            FrozenClock(datetime.fromisoformat(seeded["clock"])), run_id="first",
        )
    for key in ("final_output", "summary", "state_delta"):
        (trace / f"{key}.json").unlink()
    inspected = _invoke(tmp_path, capsys, "inspect", "first")
    assert inspected["output"] is None
    assert inspected["summary"]["status"] == "interrupted"
    assert [entry["action"] for entry in inspected["audit"]] == ["payment.transition"]


def test_inspect_recovers_hot_journal_without_changing_retained_files(tmp_path, capsys, local_provider):
    seeded = _invoke(tmp_path, capsys, "seed", "base")
    original = _invoke(tmp_path, capsys, "run", "base", "--task", "acme-list", "--id", "first")
    trace = Path(original["trace_path"])
    sandbox = tmp_path / "sandboxes" / original["run_key"] / "run.sqlite3"
    with closing(connect(sandbox)) as db:
        transition_payment(
            db, Principal("northstar_property", "simulator"), "PMT-2", "posted",
            FrozenClock(datetime.fromisoformat(seeded["clock"])), run_id="first",
        )
    subprocess.run([
        sys.executable, "-c",
        """
import os
import sqlite3
import sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute('PRAGMA cache_size=2')
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE document_versions SET content = ?", ("uncommitted" * 150000,))
os._exit(0)
""",
        str(sandbox),
    ], check=True)
    journal = sandbox.with_name(sandbox.name + "-journal")
    database_bytes, journal_bytes = sandbox.read_bytes(), journal.read_bytes()
    with closing(sqlite3.connect(sandbox.as_uri() + "?mode=ro", uri=True)) as db:
        with pytest.raises(sqlite3.OperationalError) as failed:
            db.execute("SELECT count(*) FROM audit_events")
    assert failed.value.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK
    (trace / "final_output.json").unlink()
    inspected = _invoke(tmp_path, capsys, "inspect", "first")
    assert [entry["action"] for entry in inspected["audit"]] == ["payment.transition"]
    assert sandbox.read_bytes() == database_bytes and journal.read_bytes() == journal_bytes


def test_rejects_unsafe_paths_unknown_runs_and_snapshot_mismatch(tmp_path, capsys, local_provider):
    assert "safe path component" in _invoke(tmp_path, capsys, "seed", "../escape", status=1)
    assert "safe path component" in _invoke(tmp_path, capsys, "inspect", "../escape", status=1)
    assert "Unknown run" in _invoke(tmp_path, capsys, "inspect", "missing", status=1)
    assert "Unknown snapshot" in _invoke(tmp_path, capsys, "reset", "missing", "--id", "fresh", status=1)

    _invoke(tmp_path, capsys, "seed", "base", "--task", "acme-paged-v2")
    assert "already exists" in _invoke(tmp_path, capsys, "seed", "base", status=1)
    error = _invoke(tmp_path, capsys, "run", "base", "--task", "acme-list", "--id", "first", status=1)
    assert "does not match the snapshot" in error
    assert not local_provider.identity_calls

    snapshots = tmp_path / "snapshots"
    (snapshots / "alias").symlink_to(snapshots / "base", target_is_directory=True)
    error = _invoke(tmp_path, capsys, "reset", "alias", "--id", "fresh", status=1)
    assert "Unsafe directory" in error


def test_rejects_mismatched_snapshot_metadata_and_database(tmp_path, capsys):
    _invoke(tmp_path, capsys, "seed", "base")
    directory = tmp_path / "snapshots" / "base"
    _alter(directory / "snapshot.json", clock="2028-01-01T00:00:00+00:00")
    assert "metadata does not match" in _invoke(tmp_path, capsys, "reset", "base", "--id", "fresh", status=1)

    _invoke(tmp_path, capsys, "seed", "second")
    path = tmp_path / "snapshots" / "second" / "snapshot.sqlite3"
    with path.open("ab") as stream:
        stream.write(b"tampered")
    assert "digest mismatch" in _invoke(tmp_path, capsys, "reset", "second", "--id", "fresh", status=1)


def test_inspect_rejects_ambiguous_run_id(tmp_path, capsys, local_provider):
    _invoke(tmp_path, capsys, "seed", "base")
    first = _invoke(tmp_path, capsys, "run", "base", "--task", "acme-list", "--id", "duplicate")
    second = _invoke(tmp_path, capsys, "run", "base", "--task", "acme-list", "--id", "duplicate")
    assert first["run_key"] != second["run_key"]
    assert "Ambiguous" in _invoke(tmp_path, capsys, "inspect", "duplicate", status=1)
    assert _invoke(tmp_path, capsys, "inspect", first["run_key"])["run_key"] == first["run_key"]
