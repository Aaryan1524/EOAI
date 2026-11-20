"""Load versioned experiment suites without sending grader data to the runner."""

import json
import os
import re
import sqlite3
import stat
import unicodedata
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime
from hashlib import file_digest, sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from enterprise_ops.agent.model import strict_json
from enterprise_ops.agent.runner import RunTask
from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import Capability, EscalationRestrictions, Principal
from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.fixtures.seed import seed_scenario
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, Snapshot
from enterprise_ops.storage.sqlite import connect, initialize
from enterprise_ops.tools.contracts import Id, PageSize, StrictModel
from enterprise_ops.tools.session import WriteScope


Split = Literal["adaptation", "gate", "heldout"]
Profile = Literal["enterprise_ops:v1", "enterprise_ops:v2"]
_DATASETS = Path(__file__).resolve().parents[1] / "datasets"
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.sqlite3")


class DatasetError(ValueError):
    """Invalid suite or unavailable held-out trust boundary."""


class _EvidenceSource(StrictModel):
    source_type: str
    source_id: Id


class _Decision(StrictModel):
    decision: Literal["eligible", "ineligible", "needs_review"]
    reason_codes: list[str]
    effective_due_date: str | None = None
    stated_due_date: str | None = None
    stated_due_date_differs: bool | None = None
    days_past_due: int | None = None
    outstanding_minor: int | None = None
    currency: str | None = None
    evidence_sources: list[_EvidenceSource] = Field(default_factory=list)


class _Ticket(StrictModel):
    status: str
    reason_code: str
    id: Id | None = None
    invoice_id: Id | None = None
    policy_id: Id | None = None
    policy_version: int | None = None
    created_at: str | None = None
    version: int | None = None


class _MissingInformation(StrictModel):
    code: str
    subject_id: Id | None


class _Expectations(StrictModel):
    tenant_id: Id
    customer_id: Id
    actor_id: Id
    invoices: dict[Id, _Decision]
    new_tickets: dict[Id, _Ticket]
    existing_tickets: dict[Id, Id]
    status: Literal["resolved", "partial", "needs_review", "failed"] = "resolved"
    missing_information: list[_MissingInformation] = Field(default_factory=list)
    require_customer_search_exhausted: bool = False
    invoice_search_required: bool = True
    invoice_page_size: PageSize | None = None
    invoice_statuses: list[str] = Field(default_factory=list)
    coverage_invoice_ids: list[Id] = Field(default_factory=list)
    require_existing_actions: bool = True
    customer_query: str | None = None
    review_required: bool = False
    pending_invoice_ids: list[Id] = Field(default_factory=list)


class _PublicCase(StrictModel):
    catalog_task: Id
    authorize_writes: bool
    expectations: _Expectations


class _PublicManifest(StrictModel):
    schema_version: Literal[1]
    split: Literal["adaptation", "gate"]
    cases: Annotated[list[_PublicCase], Field(min_length=1)]


class _TaskSpec(StrictModel):
    request: Annotated[str, Field(min_length=1, max_length=4096)]
    tenant_id: Id
    user_id: Id
    operation: Literal["list_invoices", "review_invoices", "escalate"]
    customer_query: str | None = None
    customer_id: Id | None = None
    invoice_ids: list[Id] | None = None
    authorized_actions: list[Literal["create_ticket"]] = Field(default_factory=list)
    threshold_days: Annotated[int, Field(ge=0)] | None = None
    exclude_disputed: bool = False


class _SnapshotSpec(StrictModel):
    filename: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.sqlite3$")]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    simulation_time: str


class _FaultSpec(StrictModel):
    tool_name: Annotated[str, Field(min_length=1)]
    attempt: Annotated[int, Field(ge=1)]
    kind: Literal["RATE_LIMITED", "TRANSIENT_FAILURE", "EMPTY_PAGE", "LOSE_RESPONSE"]


class _HeldoutCase(StrictModel):
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
    lineage: Id
    profile: Profile
    task: _TaskSpec
    snapshot: _SnapshotSpec
    faults: list[_FaultSpec] = Field(default_factory=list)
    expectations: _Expectations


class _HeldoutManifest(StrictModel):
    schema_version: Literal[1]
    split: Literal["heldout"]
    cases: Annotated[list[_HeldoutCase], Field(min_length=1)]


@dataclass(frozen=True)
class Case:
    id: str
    lineage: str
    profile: str
    task: RunTask
    faults: FaultPlan
    scenario: str | None
    catalog_task: str | None
    snapshot: Snapshot | None = field(repr=False)
    snapshot_fingerprint: str = field(repr=False)
    simulation_time: str
    expectations: dict = field(repr=False)

    def public_input(self) -> dict:
        """Return the same request and context that the runner sends to a model."""
        return {"request": self.task.request, **self.task.public_context()}


@dataclass(frozen=True)
class Suite:
    split: Split
    cases: tuple[Case, ...]
    digest: str


def load_adaptation() -> Suite:
    """Read the fixed adaptation manifest without opening the other splits."""
    return _load_public("adaptation")


def load_gate() -> Suite:
    """Read public gate/replay cases and check them against adaptation."""
    gate = _load_public("gate")
    validate_disjoint(load_adaptation(), gate)
    return gate


def load_heldout(path: Path, *, improvement_root: Path) -> Suite:
    """Import a custodian-owned suite from outside the improvement checkout."""
    path = Path(path)
    root = _private_root(path.parent, Path(improvement_root))
    manifest = _read(_private_file(root, path.name), _HeldoutManifest)
    try:
        cases = tuple(_heldout_case(entry, root) for entry in manifest.cases)
    except (ValueError, OSError, sqlite3.Error):
        raise DatasetError("Invalid held-out case or snapshot") from None
    heldout = _suite("heldout", cases)
    validate_disjoint(load_adaptation(), load_gate(), heldout)
    return heldout


def validate_disjoint(*suites: Suite) -> None:
    """Reject duplicate assignments and any overlap in cross-split fixture lineage."""
    seen: dict[tuple[str, str], int] = {}
    for index, suite in enumerate(suites):
        local: set[tuple[str, str]] = set()
        for case in suite.cases:
            unique_keys, shared_keys = _case_keys(case)
            for key in unique_keys:
                if key in local:
                    raise DatasetError(f"Duplicate {key[0]} within {suite.split}")
                local.add(key)
            for key in (*unique_keys, *shared_keys):
                if key in seen and seen[key] != index:
                    raise DatasetError(f"Cross-split overlap in {key[0]}")
                seen[key] = index


def _case_keys(case: Case) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    context = case.task.public_context()
    context.pop("authorized_actions")
    assignment = {
        "principal": asdict(case.task.principal),
        "request": _normalize(case.task.request),
        **context,
    }
    if case.task.customer_query is not None:
        assignment["customer_query"] = _normalize(case.task.customer_query)
    unique = [("case ID", case.id.casefold()), ("task assignment", _hash(assignment))]
    if case.catalog_task is not None:
        unique.append(("catalog task", case.catalog_task))
    shared = [("fixture lineage", _normalize(case.lineage)), ("snapshot contents", case.snapshot_fingerprint)]
    return unique, shared


def _suite(split: Split, cases: tuple[Case, ...]) -> Suite:
    digest = _hash({
        "schema_version": 1,
        "split": split,
        "cases": [{
            "id": case.id,
            "lineage": case.lineage,
            "catalog_task": case.catalog_task,
            "scenario": case.scenario,
            "profile": case.profile,
            "snapshot_fingerprint": case.snapshot_fingerprint,
            "snapshot_sha256": case.snapshot.digest if case.snapshot else None,
            "simulation_time": case.simulation_time,
            "principal": asdict(case.task.principal),
            "input": case.public_input(),
            "scope": case.task.write_scope.digest if case.task.write_scope else None,
            "faults": [asdict(step) for step in case.faults.steps],
            "expectations": case.expectations,
        } for case in cases],
    })
    suite = Suite(split, cases, digest)
    validate_disjoint(suite)
    return suite


def _load_public(split: Literal["adaptation", "gate"]) -> Suite:
    manifest = _read(_DATASETS / f"{split}.json", _PublicManifest)
    if manifest.split != split:
        raise DatasetError(f"Expected the {split} split")
    cases = []
    fingerprints = {}
    for entry in manifest.cases:
        try:
            catalog = load_task(entry.catalog_task, authorize_writes=entry.authorize_writes)
            if catalog.scenario not in fingerprints:
                with closing(connect(":memory:")) as connection:
                    initialize(connection)
                    clock = seed_scenario(connection, catalog.scenario)
                    fingerprints[catalog.scenario] = _database_fingerprint(connection), clock
            fingerprint, clock = fingerprints[catalog.scenario]
            _validate_expectations(catalog.task, entry.expectations)
        except (ValueError, sqlite3.Error) as error:
            raise DatasetError(f"Invalid {split} catalog case: {entry.catalog_task}") from error
        cases.append(Case(
            id=catalog.task.id,
            lineage="baseline" if catalog.scenario == "acme-paged-v2" else catalog.scenario,
            profile=catalog.profile,
            task=catalog.task,
            faults=catalog.faults,
            scenario=catalog.scenario,
            catalog_task=entry.catalog_task,
            snapshot=None,
            snapshot_fingerprint=fingerprint,
            simulation_time=clock.instant.isoformat(),
            expectations=entry.expectations.model_dump(exclude_unset=True),
        ))
    return _suite(split, tuple(cases))


def _heldout_case(entry: _HeldoutCase, root: Path) -> Case:
    spec = entry.snapshot
    if not _FILENAME.fullmatch(spec.filename) or not _HEX_DIGEST.fullmatch(spec.sha256):
        raise DatasetError("Invalid held-out snapshot reference")
    path = _private_file(root, spec.filename)
    if any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise DatasetError("Held-out snapshot must be self-contained")
    with path.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != spec.sha256:
            raise DatasetError("Held-out snapshot digest mismatch")
    clock = FrozenClock(datetime.fromisoformat(spec.simulation_time))
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        fingerprint = _database_fingerprint(connection)
    snapshot = Snapshot(entry.id, path, spec.sha256, entry.profile, clock)
    task = _task(entry.id, entry.task)
    _validate_expectations(task, entry.expectations)
    return Case(
        id=entry.id, lineage=entry.lineage, profile=entry.profile, task=task,
        faults=FaultPlan(tuple(FaultStep(step.tool_name, step.attempt, FaultKind(step.kind)) for step in entry.faults)),
        scenario=None, catalog_task=None, snapshot=snapshot, snapshot_fingerprint=fingerprint,
        simulation_time=clock.instant.isoformat(),
        expectations=entry.expectations.model_dump(exclude_unset=True),
    )


def _task(task_id: str, spec: _TaskSpec) -> RunTask:
    principal = Principal(spec.tenant_id, spec.user_id)
    invoices = None if spec.invoice_ids is None else frozenset(spec.invoice_ids)
    scope = None
    if invoices is not None and len(invoices) != len(spec.invoice_ids):
        raise DatasetError("Duplicate task invoice ID")
    if len(set(spec.authorized_actions)) != len(spec.authorized_actions):
        raise DatasetError("Duplicate task action")
    if spec.authorized_actions:
        if spec.operation != "escalate" or spec.customer_id is None:
            raise DatasetError("Write authorization requires a scoped escalation")
        scope = WriteScope(
            principal, f"dataset-{task_id}", spec.customer_id,
            frozenset(Capability(action) for action in spec.authorized_actions), invoices,
            EscalationRestrictions(spec.threshold_days, spec.exclude_disputed),
        )
    elif spec.threshold_days is not None or spec.exclude_disputed:
        raise DatasetError("Escalation restrictions require a write scope")
    return RunTask(
        task_id, spec.request, principal, spec.operation,
        customer_query=spec.customer_query, customer_id=spec.customer_id, invoice_ids=invoices, write_scope=scope,
    )


def _validate_expectations(task: RunTask, expected: _Expectations) -> None:
    if (
        expected.tenant_id != task.principal.tenant_id
        or expected.actor_id != task.principal.user_id
        or (task.account is not None and expected.customer_id != task.account)
    ):
        raise DatasetError("Grading expectations contradict the assignment")
    if expected.new_tickets and not (task.write_scope and Capability.CREATE_TICKET in task.write_scope.capabilities):
        raise DatasetError("Grading expectations require an unauthorized action")
    invoice_ids = (
        set(expected.coverage_invoice_ids)
        if "coverage_invoice_ids" in expected.model_fields_set
        else set(expected.invoices)
    )
    if len(set(expected.coverage_invoice_ids)) != len(expected.coverage_invoice_ids):
        raise DatasetError("Duplicate expected coverage invoice ID")
    if (
        not (expected.invoices.keys() | expected.new_tickets.keys() | expected.existing_tickets.keys()) <= invoice_ids
        or (task.assigned_invoices is not None and invoice_ids != task.assigned_invoices)
    ):
        raise DatasetError("Grading expectations contradict the invoice scope")


def _private_root(root: Path, improvement_root: Path) -> Path:
    try:
        improvement_root = improvement_root.resolve(strict=True)
        if not improvement_root.is_dir():
            raise DatasetError("Improvement workspace must be a directory")
        if root.is_symlink():
            raise DatasetError("Held-out storage must be a real directory")
        root = root.resolve(strict=True)
        if root.is_relative_to(improvement_root) or improvement_root.is_relative_to(root):
            raise DatasetError("Held-out storage must be outside the improvement workspace")
        if any(os.path.lexists(parent / ".git") for parent in (root, *root.parents)):
            raise DatasetError("Held-out storage must be outside Git checkouts")
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DatasetError("Held-out storage must be custodian-owned and private")
    except (OSError, RuntimeError):
        raise DatasetError("Held-out storage boundary cannot be verified") from None
    return root


def _private_file(root: Path, filename: str) -> Path:
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise DatasetError("Invalid held-out file reference")
    path = root / filename
    try:
        info = path.lstat()
    except OSError:
        raise DatasetError("Held-out input unavailable") from None
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
        or info.st_mode & 0o077 or info.st_nlink != 1
    ):
        raise DatasetError("Held-out files must be private, custodian-owned regular files")
    return path


def _read[M](path: Path, schema: type[M]) -> M:
    try:
        value = strict_json(path.read_bytes())
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
            raise DatasetError("Invalid manifest version")
        return schema.model_validate(value)
    except (OSError, ValueError):
        raise DatasetError("Invalid or unreadable dataset manifest") from None


def _database_fingerprint(connection: sqlite3.Connection) -> str:
    # Ignore file layout and insert order when looking for a fixture recycled across splits.
    return _hash(sorted(connection.iterdump()))


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _hash(value: object) -> str:
    return sha256(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("ascii")).hexdigest()
