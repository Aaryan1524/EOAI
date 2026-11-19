"""Bind tool calls to an authorized sandbox session."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from math import inf
from time import monotonic
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from enterprise_ops.domain.entities import Principal
from enterprise_ops.sandbox import FaultKind, FaultPlan, RunSandbox, SandboxRetired
from enterprise_ops.storage.audit import canonical, stamp
from enterprise_ops.storage.repository import Forbidden, NotFound, authorize
from enterprise_ops.storage.sqlite import DEFAULT_BUSY_TIMEOUT, connect, transaction
from enterprise_ops.tools.actions import ACTION_TOOLS, PreviewResult, Receipt
from enterprise_ops.tools.contracts import (
    Page, PageArgs, StrictModel, ToolDefinition, ToolError, ToolFailure, ToolMeta, ToolResponse,
)
from enterprise_ops.tools.reads import READ_TOOLS
from enterprise_ops.tools.session import Observation, Session, ToolCall, WriteScope


TOOL_PROFILE = "enterprise_ops:v1"
TOOLS = READ_TOOLS | ACTION_TOOLS


class ToolGateway:
    def __init__(
        self,
        sandbox: RunSandbox,
        principal: Principal,
        *,
        write_scope: WriteScope | None = None,
        faults: FaultPlan | None = None,
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        if sandbox.profile != TOOL_PROFILE:
            raise ValueError("Unsupported tool profile")
        if write_scope is not None and write_scope.principal != principal:
            raise ValueError("Write scope belongs to a different principal")
        self._session = Session(sandbox, principal, write_scope, elapsed)
        self._faults = faults.fresh() if faults is not None else FaultPlan()

    def declarations(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "schema_version": "v1",
                "arguments": definition.arguments.model_json_schema(),
                "result": definition.result.model_json_schema(),
            }
            for name, definition in TOOLS.items()
        ]

    def observation(self, evidence_id: str) -> Observation | None:
        if not self._session.sandbox.active:
            return None
        return self._session.observed(evidence_id)

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        schema_version: str = "v1",
        timeout: float | None = None,
    ) -> ToolResponse:
        """Invoke a declared tool with an optional elapsed-time budget in seconds."""
        started = monotonic() if timeout is not None else 0.0
        deadline = None
        known_receipt = False
        result = None
        sandbox = self._session.sandbox
        call_id = f"call_{uuid4().hex}"
        revision = None
        definition = TOOLS.get(name) if isinstance(name, str) else None
        tool_name = name if definition is not None else "unknown"
        try:
            if timeout is not None:
                if type(timeout) not in (int, float) or not 0 < timeout < inf:
                    raise _deadline_failure()
                deadline = started + timeout
                _remaining(deadline)
            if not sandbox.active:
                raise ToolFailure("PRECONDITION_FAILED", "Sandbox session is retired")
            if definition is None or schema_version != "v1":
                raise ToolFailure("SCHEMA_MISMATCH", "Tool or schema version is not declared")
            try:
                args = definition.arguments.model_validate(arguments)
            except ValidationError as error:
                raise ToolFailure("VALIDATION_ERROR", "Arguments do not match the tool schema") from error
            with sandbox.operation():
                if not sandbox.path.is_file():
                    raise ToolFailure("PRECONDITION_FAILED", "Sandbox database is unavailable")
                with (
                    closing(connect(sandbox.path, timeout=_lock_timeout(deadline))) as db,
                    _tool_transaction(db, write=definition.write, deadline=deadline),
                ):
                    if deadline is not None and not definition.write:
                        _refresh_lock_timeout(db, deadline)
                    authorize(db, self._session.principal, definition.capability)
                    if deadline is not None:
                        # Authorization acquired the read lock. BEGIN IMMEDIATE acquired the write reservation.
                        db.execute("PRAGMA busy_timeout = 0")
                    tenant = db.execute(
                        "SELECT timezone, revision FROM tenants WHERE id = ?",
                        (self._session.principal.tenant_id,),
                    ).fetchone()
                    revision = tenant["revision"]
                    call = ToolCall(db, self._session, name, call_id, tenant["timezone"], revision)
                    if deadline is not None:
                        _remaining(deadline)
                    fault = self._faults.next_for(name)
                    result = _dispatch(call, definition, args, fault)
                known_receipt = isinstance(result, Receipt)
                revision = call.revision
                if deadline is not None and not known_receipt:
                    _remaining(deadline)
                if fault == FaultKind.LOSE_RESPONSE:
                    raise ToolFailure("TRANSIENT_FAILURE", "Tool response was lost", retryable=True)
                self._session.observe(call.observations)
                return ToolResponse[definition.result](
                    ok=True, data=result, error=None,
                    meta=self._meta(tool_name, call_id, revision, list(call.observations)),
                )
        except ToolFailure as failure:
            error = failure.error
        except SandboxRetired:
            error = ToolError(code="PRECONDITION_FAILED", message="Sandbox session is retired", retryable=False)
        except Forbidden:
            error = ToolError(code="PERMISSION_DENIED", message="Operation is not authorized", retryable=False)
        except NotFound:
            error = ToolError(code="NOT_FOUND", message="Scoped record was not found", retryable=False)
        except sqlite3.IntegrityError:
            error = ToolError(code="CONFLICT", message="State rejected the operation", retryable=False)
        except sqlite3.OperationalError as failure:
            error_code = getattr(failure, "sqlite_errorcode", 0)
            if error_code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                error = ToolError(code="TRANSIENT_FAILURE", message="Sandbox is busy", retryable=True)
            else:
                error = _unknown_error()
        except (sqlite3.DatabaseError, ValueError, TypeError):
            error = _unknown_error()
        if deadline is not None and not known_receipt and monotonic() >= deadline:
            if isinstance(result, PreviewResult) and result.preview_token is not None:
                with self._session.lock:
                    self._session.previews.pop(result.preview_token, None)
            error = _deadline_failure().error
        return ToolResponse(
            ok=False, data=None, error=error, meta=self._meta(tool_name, call_id, revision, []),
        )

    def _meta(self, tool: str, call_id: str, revision: int | None, evidence: list[str]) -> ToolMeta:
        sandbox = self._session.sandbox
        return ToolMeta(
            tool_name=tool, profile=sandbox.profile, sandbox_id=sandbox.id, state_revision=revision,
            observed_at=stamp(sandbox.clock.instant), call_id=call_id, evidence_ids=evidence,
        )


def _dispatch(
    call: ToolCall, definition: ToolDefinition, args: StrictModel, fault: FaultKind | None
) -> StrictModel:
    if fault == FaultKind.RATE_LIMITED:
        raise ToolFailure("RATE_LIMITED", "Sandbox rate limit", retryable=True, retry_after_ms=25)
    if fault == FaultKind.TRANSIENT_FAILURE:
        raise ToolFailure("TRANSIENT_FAILURE", "Temporary sandbox failure", retryable=True)
    result = definition.result.model_validate(definition.handler(call, args))
    if fault == FaultKind.EMPTY_PAGE and isinstance(args, PageArgs) and isinstance(result, Page) and result.items:
        last_id = call.seek(args)
        next_cursor = call.cursor(args, last_id, empty=True)
        call.observations.clear()
        call.evidence(
            "query", call.name, 0,
            canonical([args.model_dump(exclude={"cursor"}), last_id, [], False]),
            invoice_id=getattr(args, "invoice_id", None),
        )
        return definition.result.model_validate({"items": [], "next_cursor": next_cursor})
    return result


@contextmanager
def _tool_transaction(
    db: sqlite3.Connection, *, write: bool, deadline: float | None
) -> Iterator[None]:
    if deadline is not None:
        _refresh_lock_timeout(db, deadline)
        db.set_progress_handler(lambda: monotonic() >= deadline, 100)
    with transaction(db, write=write):
        try:
            yield
        finally:
            if deadline is not None:
                # Rollback must run even when a SQLite statement was interrupted.
                db.set_progress_handler(None, 0)
        if deadline is not None and db.total_changes:
            # An existing receipt does not modify state. Reusing an approval can still insert a receipt.
            _refresh_lock_timeout(db, deadline)
            _remaining(deadline)


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _deadline_failure()
    return remaining


def _lock_timeout(deadline: float | None) -> float:
    if deadline is None:
        return DEFAULT_BUSY_TIMEOUT
    return min(DEFAULT_BUSY_TIMEOUT, _remaining(deadline))


def _refresh_lock_timeout(db: sqlite3.Connection, deadline: float) -> None:
    db.execute(f"PRAGMA busy_timeout = {int(_lock_timeout(deadline) * 1000)}")


def _deadline_failure() -> ToolFailure:
    return ToolFailure("TRANSIENT_FAILURE", "Tool deadline expired", retryable=True)


def _unknown_error() -> ToolError:
    return ToolError(code="UNKNOWN_ERROR", message="Tool could not produce a valid result", retryable=False)
