from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from hashlib import sha256
from threading import Event
from time import monotonic, sleep

import pytest

from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, SandboxRetired, seed_snapshot
from enterprise_ops.storage.sqlite import connect


def _revision(path):
    with closing(connect(path)) as connection:
        return connection.execute("SELECT MAX(revision) FROM tenants").fetchone()[0]


def test_seeds_a_verifiable_baseline_with_its_profile_and_clock(tmp_path):
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline.v1", profile="enterprise_ops:custom")

    assert snapshot.id == "baseline.v1"
    assert snapshot.path.is_file()
    assert snapshot.digest == sha256(snapshot.path.read_bytes()).hexdigest()
    assert snapshot.profile == "enterprise_ops:custom"
    assert snapshot.clock.business_date("America/New_York") == date(2026, 11, 19)
    assert _revision(snapshot.path) == 1


def test_concurrent_seed_publish_never_overwrites_existing_snapshot(tmp_path):
    def seed():
        try:
            return seed_snapshot(tmp_path, "baseline")
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [attempt.result() for attempt in (executor.submit(seed), executor.submit(seed))]

    snapshot, = [result for result in results if result is not None]
    assert list(tmp_path.iterdir()) == [snapshot.path]
    original = snapshot.path.read_bytes()
    with pytest.raises(FileExistsError):
        seed_snapshot(tmp_path, "baseline")
    assert snapshot.path.read_bytes() == original


def test_clones_are_independent_even_when_the_run_ids_match(tmp_path):
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline")
    run_root = tmp_path / "runs"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(lambda _: snapshot.clone(run_root, "run-1"), range(2))

    assert first.id != second.id
    assert first.run_id == second.run_id == "run-1"
    assert first.id != first.run_id
    assert first.path != second.path
    assert first.profile == second.profile == snapshot.profile == "enterprise_ops:v1"
    assert first.clock == second.clock == snapshot.clock
    assert first.path.read_bytes() == second.path.read_bytes() == snapshot.path.read_bytes()

    with closing(connect(first.path)) as connection:
        connection.execute("UPDATE tenants SET revision = 5")
    assert _revision(first.path) == 5
    assert _revision(second.path) == _revision(snapshot.path) == 1


def test_reset_retires_the_session_and_isolates_existing_connections(tmp_path):
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline")
    initial = snapshot.clone(tmp_path / "runs", "run-1")
    assert initial.active

    with closing(connect(initial.path)) as old_connection:
        old_connection.execute("BEGIN IMMEDIATE")
        old_connection.execute("UPDATE tenants SET revision = 2")
        restored = initial.reset()
        old_connection.commit()

    assert initial.active is False
    assert restored.active
    assert (restored.run_id, restored.profile, restored.clock) == (
        initial.run_id, initial.profile, initial.clock
    )
    assert restored.id != initial.id
    assert restored.path != initial.path
    assert restored.path.read_bytes() == snapshot.path.read_bytes()
    assert _revision(initial.path) == 2
    assert _revision(restored.path) == 1
    with pytest.raises(SandboxRetired, match="retired"):
        initial.reset()
    assert restored.reset().active


def test_concurrent_resets_cannot_replace_one_session_twice(tmp_path):
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline")
    initial = snapshot.clone(tmp_path / "runs", "run-1")

    def reset():
        try:
            return initial.reset()
        except SandboxRetired:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [attempt.result() for attempt in (executor.submit(reset), executor.submit(reset))]

    replacement, = [result for result in results if result is not None]
    assert replacement.active
    assert initial.active is False


def test_reset_drains_concurrent_operations_and_blocks_new_admissions(tmp_path):
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline")
    initial = snapshot.clone(tmp_path / "runs", "run-1")
    admitted = Event()
    release = Event()

    def hold_operation():
        with initial.operation():
            admitted.set()
            assert release.wait(timeout=5)
            with closing(connect(initial.path)) as connection:
                connection.execute("UPDATE tenants SET revision = 2")

    with ThreadPoolExecutor(max_workers=2) as executor:
        operation = executor.submit(hold_operation)
        try:
            assert admitted.wait(timeout=5)
            with initial.operation():
                reset = executor.submit(initial.reset)
                deadline = monotonic() + 5
                while initial.active and monotonic() < deadline:
                    sleep(0.001)
                assert initial.active is False
                assert not reset.done()
                with pytest.raises(SandboxRetired):
                    with initial.operation():
                        pytest.fail("Resetting sandbox admitted an operation")
            assert not reset.done()
        finally:
            release.set()

        operation.result(timeout=5)
        replacement = reset.result(timeout=5)

    assert _revision(initial.path) == 2
    with pytest.raises(SandboxRetired):
        with initial.operation():
            pytest.fail("Retired sandbox admitted an operation")
    with replacement.operation():
        assert _revision(replacement.path) == 1


def test_operation_failure_releases_reset_hold(tmp_path):
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline")
    initial = snapshot.clone(tmp_path / "runs", "run-1")
    with pytest.raises(ValueError, match="call failed"):
        with initial.operation():
            raise ValueError("call failed")

    assert initial.reset().active


def test_tampered_baseline_cannot_be_cloned_and_failed_reset_keeps_session(tmp_path):
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline")
    run_root = tmp_path / "runs"
    run = snapshot.clone(run_root, "run-1")
    with snapshot.path.open("ab") as stream:
        stream.write(b"unexpected data")

    with pytest.raises(ValueError, match="digest mismatch"):
        snapshot.clone(run_root, "run-2")
    with pytest.raises(ValueError, match="digest mismatch"):
        run.reset()
    assert list(run_root.iterdir()) == [run.path.parent]
    assert run.active
    with run.operation():
        assert _revision(run.path) == 1


@pytest.mark.parametrize("identifier", ["", ".", "..", "a/../b", "a\\b", "/absolute", "x" * 129])
def test_snapshot_and_run_ids_must_be_safe_path_components(tmp_path, identifier):
    snapshot_root = tmp_path / "snapshots"
    run_root = tmp_path / "runs"
    with pytest.raises(ValueError, match="snapshot_id"):
        seed_snapshot(snapshot_root, identifier)
    assert not snapshot_root.exists()

    snapshot = seed_snapshot(snapshot_root, "baseline")
    with pytest.raises(ValueError, match="run_id"):
        snapshot.clone(run_root, identifier)
    assert not run_root.exists()


def test_fault_plan_replays_per_tool_attempts_with_independent_counters():
    plan = FaultPlan((
        FaultStep("invoice.search", 3, FaultKind.EMPTY_PAGE),
        FaultStep("invoice.search", 1, FaultKind.RATE_LIMITED),
        FaultStep("ticket.create", 1, FaultKind.LOSE_RESPONSE),
        FaultStep("ticket.create", 2, FaultKind.TRANSIENT_FAILURE),
    ))

    for attempt_plan in (plan, plan.fresh()):
        assert attempt_plan.next_for("clock.now") is None
        assert attempt_plan.next_for("invoice.search") == FaultKind.RATE_LIMITED
        assert attempt_plan.next_for("ticket.create") == FaultKind.LOSE_RESPONSE
        assert attempt_plan.next_for("invoice.search") is None
        assert attempt_plan.next_for("ticket.create") == FaultKind.TRANSIENT_FAILURE
        assert attempt_plan.next_for("invoice.search") == FaultKind.EMPTY_PAGE
        assert attempt_plan.next_for("invoice.search") is None

    assert plan.fresh().next_for("invoice.search") == FaultKind.RATE_LIMITED
    assert plan.next_for("invoice.search") is None
    assert FaultPlan().next_for("invoice.search") is None


def test_fault_attempts_are_counted_once_under_concurrency():
    plan = FaultPlan((FaultStep("invoice.search", 25, FaultKind.RATE_LIMITED),))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(plan.next_for, ["invoice.search"] * 50))

    assert results.count(FaultKind.RATE_LIMITED) == 1
    assert results.count(None) == 49


@pytest.mark.parametrize("attempt", [0, -1, True, 1.0])
def test_fault_steps_require_positive_integer_attempts(attempt):
    with pytest.raises(ValueError, match="attempt"):
        FaultStep("invoice.search", attempt, FaultKind.EMPTY_PAGE)


def test_fault_plan_rejects_duplicate_tool_attempts():
    with pytest.raises(ValueError, match="Duplicate fault step"):
        FaultPlan((
            FaultStep("invoice.search", 1, FaultKind.RATE_LIMITED),
            FaultStep("invoice.search", 1, FaultKind.EMPTY_PAGE),
        ))
