"""Create isolated SQLite run databases and replay deterministic tool faults."""

import os
import re
import shutil
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import file_digest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Condition, Lock
from uuid import uuid4

from enterprise_ops.clock import FrozenClock
from enterprise_ops.fixtures.seed import seed_scenario
from enterprise_ops.storage.sqlite import connect, initialize


_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class SandboxRetired(RuntimeError):
    """The session is resetting or has already been replaced."""


@dataclass(frozen=True)
class Snapshot:
    id: str
    path: Path
    digest: str
    profile: str
    clock: FrozenClock

    def clone(self, root: Path, run_id: str) -> "RunSandbox":
        """Publish a verified private copy for a new run session."""
        _check_component(run_id, "run_id")
        root = _prepare_root(root)
        identity = f"sandbox-{uuid4().hex}"
        directory = root / f"{run_id}-{identity}"
        directory.mkdir(mode=0o700)
        path = directory / "run.sqlite3"
        try:
            staged = directory / ".staged"
            shutil.copyfile(self.path, staged)
            if _digest(staged) != self.digest:
                raise ValueError(f"Snapshot digest mismatch for {self.id}")
            os.replace(staged, path)
        except BaseException:
            shutil.rmtree(directory)
            raise
        return RunSandbox(identity, run_id, path, self.profile, self.clock, self, root)


@dataclass
class _Lifecycle:
    condition: Condition = field(default_factory=Condition)
    accepting: bool = True
    operations: int = 0


@dataclass(frozen=True)
class RunSandbox:
    id: str
    run_id: str
    path: Path
    profile: str
    clock: FrozenClock
    _source: Snapshot = field(repr=False, compare=False)
    _root: Path = field(repr=False, compare=False)
    _lifecycle: _Lifecycle = field(default_factory=_Lifecycle, init=False, repr=False, compare=False)

    @property
    def active(self) -> bool:
        with self._lifecycle.condition:
            return self._lifecycle.accepting

    @contextmanager
    def operation(self) -> Iterator[None]:
        """Hold the current session open for a managed operation."""
        lifecycle = self._lifecycle
        with lifecycle.condition:
            if not lifecycle.accepting:
                raise SandboxRetired("Sandbox session is resetting or retired")
            lifecycle.operations += 1
        try:
            yield
        finally:
            with lifecycle.condition:
                lifecycle.operations -= 1
                if lifecycle.operations == 0:
                    lifecycle.condition.notify_all()

    def reset(self) -> "RunSandbox":
        """Restore the source snapshot under a new identity and retire this session.

        Call from outside a managed operation so admitted operations can finish.
        Raw SQLite connections still refer to this session's database.
        """
        lifecycle = self._lifecycle
        with lifecycle.condition:
            if not lifecycle.accepting:
                raise SandboxRetired("Sandbox session is resetting or retired")
            lifecycle.accepting = False
        try:
            with lifecycle.condition:
                lifecycle.condition.wait_for(lambda: lifecycle.operations == 0)
            return self._source.clone(self._root, self.run_id)
        except BaseException:
            with lifecycle.condition:
                lifecycle.accepting = True
            raise


def seed_snapshot(
    root: Path,
    snapshot_id: str,
    *,
    profile: str = "enterprise_ops:v1",
    scenario: str = "baseline",
) -> Snapshot:
    """Create and publish a closed fixture database without overwriting a snapshot."""
    _check_component(snapshot_id, "snapshot_id")
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("profile must be a nonempty string")
    root = _prepare_root(root)
    path = root / f"{snapshot_id}.sqlite3"
    with TemporaryDirectory(prefix=".seed-", dir=root) as staging:
        staged = Path(staging) / "snapshot.sqlite3"
        with closing(connect(staged)) as connection:
            initialize(connection)
            clock = seed_scenario(connection, scenario)
        digest = _digest(staged)
        staged.chmod(0o600)
        os.link(staged, path)
    return Snapshot(snapshot_id, path, digest, profile, clock)


def _check_component(value: str, name: str) -> None:
    if not isinstance(value, str) or _COMPONENT.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe path component")


def _prepare_root(root: Path) -> Path:
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


class FaultKind(StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    EMPTY_PAGE = "EMPTY_PAGE"
    LOSE_RESPONSE = "LOSE_RESPONSE"


@dataclass(frozen=True)
class FaultStep:
    tool_name: str
    attempt: int
    kind: FaultKind

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("Fault tool name must be a nonempty string")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("Fault attempt must be a positive integer")
        if not isinstance(self.kind, FaultKind):
            raise ValueError("Fault kind must be a FaultKind")


class FaultPlan:
    """Apply each scripted fault on its tool's one-based invocation count."""

    def __init__(self, steps: tuple[FaultStep, ...] = ()) -> None:
        self._steps = tuple(steps)
        self._schedule: dict[tuple[str, int], FaultKind] = {}
        for step in self._steps:
            if not isinstance(step, FaultStep):
                raise ValueError("Fault plan must contain FaultStep values")
            key = (step.tool_name, step.attempt)
            if key in self._schedule:
                raise ValueError(f"Duplicate fault step for {step.tool_name} attempt {step.attempt}")
            self._schedule[key] = step.kind
        self._attempts: dict[str, int] = {}
        self._lock = Lock()

    def next_for(self, tool_name: str) -> FaultKind | None:
        with self._lock:
            attempt = self._attempts.get(tool_name, 0) + 1
            self._attempts[tool_name] = attempt
            return self._schedule.get((tool_name, attempt))

    def fresh(self) -> "FaultPlan":
        """Recreate the script with independent attempt counters."""
        return FaultPlan(self._steps)

    @property
    def steps(self) -> tuple[FaultStep, ...]:
        return self._steps
