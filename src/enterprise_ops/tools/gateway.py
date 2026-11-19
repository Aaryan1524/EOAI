"""Bind tool calls to an authorized sandbox session."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from time import monotonic
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from enterprise_ops.domain.entities import Principal
from enterprise_ops.sandbox import FaultKind, FaultPlan, RunSandbox, SandboxRetired
from enterprise_ops.storage.audit import canonical, stamp
from enterprise_ops.storage.repository import Forbidden, NotFound, authorize
from enterprise_ops.storage.sqlite import connect, transaction
from enterprise_ops.tools.actions import ACTION_TOOLS
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
        self, name: str, arguments: dict[str, Any], *, schema_version: str = "v1"
    ) -> ToolResponse:
        sandbox = self._session.sandbox
        call_id = f"call_{uuid4().hex}"
        revision = None
        definition = TOOLS.get(name) if isinstance(name, str) else None
        tool_name = name if definition is not None else "unknown"
        try:
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
                with closing(connect(sandbox.path)) as db, transaction(db, write=definition.write):
                    authorize(db, self._session.principal, definition.capability)
                    tenant = db.execute(
                        "SELECT timezone, revision FROM tenants WHERE id = ?",
                        (self._session.principal.tenant_id,),
                    ).fetchone()
                    revision = tenant["revision"]
                    call = ToolCall(db, self._session, name, call_id, tenant["timezone"], revision)
                    fault = self._faults.next_for(name)
                    result = _dispatch(call, definition, args, fault)
                revision = call.revision
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


def _unknown_error() -> ToolError:
    return ToolError(code="UNKNOWN_ERROR", message="Tool could not produce a valid result", retryable=False)
