"""Preview, commit, and read back authorized simulated actions."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field

from enterprise_ops.domain.entities import Capability, Principal
from enterprise_ops.domain.policy import Assessment
from enterprise_ops.sandbox import RunSandbox
from enterprise_ops.storage.audit import append_event, bump_revision, canonical, stamp
from enterprise_ops.storage.repository import Forbidden, NotFound, authorize, load_assessment
from enterprise_ops.storage.sqlite import connect, transaction
from enterprise_ops.tools.contracts import (
    Id,
    Page,
    PageArgs,
    Revision,
    StrictModel,
    ToolDefinition,
    ToolFailure,
    Version,
)
from enterprise_ops.tools.session import Preview, ToolCall, WriteScope, assessment_locator, digest


TicketStatus = Literal["open", "in_progress", "resolved", "closed"]
Reason = Literal["OVERDUE_WITH_OUTSTANDING_BALANCE"]
PREVIEW_SECONDS = 60.0
COMMAND_CAPABILITY = {
    "ticket.create": Capability.CREATE_TICKET,
    "approval.request": Capability.REQUEST_APPROVAL,
}


class TicketSearchArgs(PageArgs):
    invoice_id: Id
    statuses: Annotated[list[TicketStatus], Field(min_length=1, max_length=4)] | None = None


class TicketArgs(StrictModel):
    ticket_id: Id


class ApprovalArgs(StrictModel):
    approval_id: Id


class PreviewArgs(StrictModel):
    invoice_id: Id
    reason_code: Reason
    evidence_ids: Annotated[list[Id], Field(min_length=1, max_length=25)]
    approval_id: Id | None = None


class CommitArgs(StrictModel):
    preview_token: Id
    idempotency_key: Id
    expected_tenant_revision: Revision
    expected_invoice_version: Version
    expected_policy_version: Version


class PreviewResult(StrictModel):
    invoice_id: Id
    decision: Literal["ready_to_create", "approval_required", "already_active", "blocked", "needs_review"]
    reason_codes: list[str]
    tenant_revision: Revision
    invoice_version: Version
    policy_version: Version | None
    existing_ticket_id: Id | None = None
    preview_token: Id | None = None
    idempotency_key: Id | None = None


class Receipt(StrictModel):
    invoice_id: Id
    result_id: Id
    audit_id: Id
    creating_call_id: Id
    state_revision: Revision
    replayed: bool


class TicketResult(StrictModel):
    id: Id
    invoice_id: Id
    policy_id: Id
    policy_version: Version
    status: TicketStatus
    reason_code: str
    created_at: str
    version: Version
    creation_audit_id: Id | None
    creating_call_id: Id | None


class ApprovalResult(StrictModel):
    id: Id
    invoice_id: Id
    status: Literal["pending", "approved", "rejected", "expired"]
    expires_at: str
    basis_digest: str
    consumed_ticket_id: Id | None
    version: Version
    creation_audit_id: Id
    creating_call_id: Id


def _invoice(call: ToolCall, invoice_id: str) -> sqlite3.Row:
    row = call.connection.execute(
        "SELECT customer_id, contract_id FROM invoices WHERE tenant_id = ? AND id = ?",
        (call.tenant_id, invoice_id),
    ).fetchone()
    if row is None:
        raise NotFound("Invoice not found")
    return row


def _scope(call: ToolCall, capability: Capability | None = None) -> WriteScope:
    scope = call.session.write_scope
    if scope is None or not scope.capabilities or (
        capability is not None and capability not in scope.capabilities
    ):
        raise Forbidden("Operation requires an authorized write scope")
    return scope


def _scoped_invoice(call: ToolCall, scope: WriteScope, invoice_id: str) -> sqlite3.Row:
    if scope.invoice_ids is not None and invoice_id not in scope.invoice_ids:
        raise Forbidden("Invoice is outside the write scope")
    invoice = _invoice(call, invoice_id)
    if invoice["customer_id"] != scope.customer_id:
        raise Forbidden("Invoice is outside the write scope")
    return invoice


def _assess(call: ToolCall, invoice_id: str) -> Assessment:
    return load_assessment(call.connection, call.tenant_id, invoice_id, call.clock, call.restrictions)


def _require_evidence(call: ToolCall, assessment: Assessment, identifiers: tuple[str, ...]) -> None:
    terms = set()
    for reference in assessment.terms.source_refs:
        tenant, document, version, span, content_hash, start, end = json.loads(reference)
        if tenant == call.tenant_id:
            terms.add((document, version, json.dumps([span, content_hash, start, end], separators=(",", ":"))))
    exact_assessment = assessment_locator(assessment)
    assessment_observed = False
    for identifier in identifiers:
        observation = call.session.observed(identifier)
        if observation is None:
            raise ToolFailure("PRECONDITION_FAILED", "Evidence was not observed in this session")
        if observation.source_type == "document_span":
            if (observation.source_id, observation.source_version, observation.locator) not in terms:
                raise ToolFailure("PRECONDITION_FAILED", "Evidence does not support this invoice")
        elif observation.invoice_id != assessment.invoice_id or observation.revision != call.revision:
            raise ToolFailure("PRECONDITION_FAILED", "Evidence is for a different invoice or revision")
        elif observation.source_type == "invoice_assessment":
            if observation.locator != exact_assessment or observation.source_version != assessment.invoice_version:
                raise ToolFailure("PRECONDITION_FAILED", "Evidence does not match the current assessment")
            assessment_observed = True
    if not assessment_observed:
        raise ToolFailure("PRECONDITION_FAILED", "Cite an assessment at the current revision")


def _current_ticket(call: ToolCall, invoice_id: str) -> str | None:
    row = call.connection.execute(
        "SELECT id FROM escalation_tickets WHERE tenant_id = ? AND invoice_id = ? "
        "AND status IN ('open', 'in_progress')",
        (call.tenant_id, invoice_id),
    ).fetchone()
    return row["id"] if row else None


def _basis(call: ToolCall, scope: WriteScope, assessment: Assessment, invoice: sqlite3.Row) -> str:
    connection = call.connection
    relevant = {
        "customer": connection.execute(
            "SELECT id, version FROM customers WHERE tenant_id = ? AND id = ?",
            (call.tenant_id, invoice["customer_id"]),
        ).fetchone(),
        "contract": connection.execute(
            "SELECT id, version FROM contracts WHERE tenant_id = ? AND id = ?",
            (call.tenant_id, invoice["contract_id"]),
        ).fetchone(),
    }
    ledger = [
        (tuple(row) for row in connection.execute(query, parameters))
        for query, parameters in (
            ("SELECT id, version FROM payments WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",
             (call.tenant_id, assessment.invoice_id)),
            ("SELECT id, version FROM disputes WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",
             (call.tenant_id, assessment.invoice_id)),
            ("SELECT id, version FROM payment_clauses WHERE tenant_id = ? AND contract_id = ? ORDER BY id",
             (call.tenant_id, invoice["contract_id"])),
            ("SELECT id, version FROM amendments WHERE tenant_id = ? AND contract_id = ? ORDER BY id",
             (call.tenant_id, invoice["contract_id"])),
        )
    ]
    return digest({
        "action": "ticket.create", "tenant": call.tenant_id, "requester": call.principal.user_id,
        "scope": scope.digest, "assessment": asdict(assessment),
        "parents": {name: None if row is None else tuple(row) for name, row in relevant.items()},
        "inputs": [list(rows) for rows in ledger],
    })


def _approval_row(
    db: sqlite3.Connection, sandbox: RunSandbox, tenant_id: str, approval_id: str
) -> sqlite3.Row:
    row = db.execute(
        "SELECT r.*, a.call_id FROM approval_requests r JOIN audit_events a "
        "ON a.tenant_id = r.tenant_id AND a.id = r.creation_audit_id "
        "WHERE r.tenant_id = ? AND r.sandbox_id = ? AND r.run_id = ? AND r.id = ?",
        (tenant_id, sandbox.id, sandbox.run_id, approval_id),
    ).fetchone()
    if row is None:
        raise NotFound("Approval not found")
    return row


def _approval_ok(call: ToolCall, scope: WriteScope, invoice_id: str, approval_id: str, basis: str) -> bool:
    row = _approval_row(call.connection, call.session.sandbox, call.tenant_id, approval_id)
    return (
        row["invoice_id"] == invoice_id and row["requested_by"] == call.principal.user_id
        and row["scope_digest"] == scope.digest and row["basis_digest"] == basis
        and row["status"] == "approved" and row["consumed_ticket_id"] is None
        and datetime.fromisoformat(row["expires_at"]) > call.clock.instant
    )


def preview_create(call: ToolCall, args: PreviewArgs) -> PreviewResult:
    scope = _scope(call)
    invoice = _scoped_invoice(call, scope, args.invoice_id)
    assessment = _assess(call, args.invoice_id)
    result = {
        "invoice_id": args.invoice_id, "tenant_revision": call.revision,
        "invoice_version": assessment.invoice_version, "policy_version": assessment.policy_version,
        "reason_codes": list(assessment.reason_codes),
    }
    if assessment.status != "eligible":
        return PreviewResult(
            **result, decision="needs_review" if assessment.status == "needs_review" else "blocked",
        )
    active_ticket = _current_ticket(call, args.invoice_id)
    if active_ticket is not None:
        return PreviewResult(**result, decision="already_active", existing_ticket_id=active_ticket)
    if args.approval_id is not None and (
        not assessment.requires_approval
        or not _approval_ok(call, scope, args.invoice_id, args.approval_id, _basis(call, scope, assessment, invoice))
    ):
        raise ToolFailure("PRECONDITION_FAILED", "Approval does not authorize this action")

    action = "approval.request" if assessment.requires_approval and args.approval_id is None else "ticket.create"
    capability = COMMAND_CAPABILITY[action]
    _scope(call, capability)
    authorize(call.connection, call.principal, capability)
    evidence = tuple(dict.fromkeys(args.evidence_ids))
    _require_evidence(call, assessment, evidence)
    token, key = f"preview_{uuid4().hex}", f"command_{uuid4().hex}"
    call.session.offer(token, Preview(
        action=action, key=key, invoice_id=args.invoice_id, scope_digest=scope.digest,
        reason_code=args.reason_code, evidence_ids=evidence, approval_id=args.approval_id,
        revision=call.revision, invoice_version=assessment.invoice_version,
        policy_version=assessment.policy_version, expires_at=call.session.elapsed() + PREVIEW_SECONDS,
    ))
    return PreviewResult(
        **result, decision="approval_required" if action == "approval.request" else "ready_to_create",
        preview_token=token, idempotency_key=key,
    )


def _command_scope(call: ToolCall) -> tuple[str, str, str, str, str]:
    return (
        call.tenant_id, call.session.sandbox.id, call.session.sandbox.run_id,
        call.principal.user_id, call.name,
    )


def _receipt(call: ToolCall, invoice_id: str, result_id: str, audit_id: str, replayed: bool) -> Receipt:
    event = call.connection.execute(
        "SELECT call_id, state_revision FROM audit_events WHERE tenant_id = ? AND id = ?",
        (call.tenant_id, audit_id),
    ).fetchone()
    call.evidence("audit_event", audit_id, event["state_revision"], call.name, invoice_id=invoice_id)
    return Receipt(
        invoice_id=invoice_id, result_id=result_id, audit_id=audit_id,
        creating_call_id=event["call_id"], state_revision=event["state_revision"], replayed=replayed,
    )


def commit(call: ToolCall, args: CommitArgs) -> Receipt:
    scope = _scope(call, COMMAND_CAPABILITY[call.name])
    command_scope = _command_scope(call)
    fingerprint = digest([scope.digest, args.model_dump()])
    cached = call.connection.execute(
        "SELECT * FROM command_receipts WHERE tenant_id = ? AND sandbox_id = ? AND run_id = ? "
        "AND actor_id = ? AND action = ? AND command_key = ?",
        (*command_scope, args.idempotency_key),
    ).fetchone()
    if cached is not None:
        _scoped_invoice(call, scope, cached["invoice_id"])
        if fingerprint != cached["fingerprint"]:
            raise ToolFailure("CONFLICT", "The idempotency key is bound to a different request")
        return _receipt(call, cached["invoice_id"], cached["result_id"], cached["audit_id"], True)

    preview = call.session.preview(args.preview_token)
    if (
        preview is None or preview.expires_at <= call.session.elapsed()
        or preview.action != call.name or preview.key != args.idempotency_key or preview.scope_digest != scope.digest
    ):
        raise ToolFailure("PRECONDITION_FAILED", "Preview is missing, expired, or for another command")
    invoice = _scoped_invoice(call, scope, preview.invoice_id)
    assessment = _assess(call, preview.invoice_id)
    if (
        args.expected_tenant_revision != call.revision or call.revision != preview.revision
        or args.expected_invoice_version != assessment.invoice_version
        or assessment.invoice_version != preview.invoice_version
        or args.expected_policy_version != assessment.policy_version
        or assessment.policy_version != preview.policy_version
    ):
        raise ToolFailure("STALE_REVISION", "Preview and current decision revisions differ")
    if assessment.status != "eligible":
        raise ToolFailure("PRECONDITION_FAILED", "Invoice is no longer eligible")
    if _current_ticket(call, preview.invoice_id) is not None:
        raise ToolFailure("CONFLICT", "An active escalation ticket already exists")
    _require_evidence(call, assessment, preview.evidence_ids)

    if call.name == "approval.request":
        if not assessment.requires_approval:
            raise ToolFailure("PRECONDITION_FAILED", "Approval is no longer required")
        result_id, audit_id, reused = _request_approval(call, preview, scope, _basis(call, scope, assessment, invoice))
    else:
        if assessment.requires_approval and (
            preview.approval_id is None or not _approval_ok(
                call, scope, preview.invoice_id, preview.approval_id, _basis(call, scope, assessment, invoice)
            )
        ):
            raise ToolFailure("PRECONDITION_FAILED", "Valid supervisor approval is required")
        result_id, audit_id = _create_ticket(call, preview, assessment)
        reused = False
    call.connection.execute(
        "INSERT INTO command_receipts "
        "(tenant_id, sandbox_id, run_id, actor_id, action, command_key, fingerprint, invoice_id, result_id, audit_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*command_scope, args.idempotency_key, fingerprint, preview.invoice_id, result_id, audit_id),
    )
    return _receipt(call, preview.invoice_id, result_id, audit_id, reused)


def _creation_event(call: ToolCall, preview: Preview, identifier: str, after: dict) -> str:
    call.revision = bump_revision(call.connection, call.tenant_id)
    return append_event(
        call.connection, call.principal, call.name, identifier, call.clock.instant, call.revision,
        reason_code=preview.reason_code, before=None,
        after={"id": identifier, "invoice_id": preview.invoice_id, **after},
        evidence_ids=preview.evidence_ids, run_id=call.session.sandbox.run_id, call_id=call.call_id,
    )


def _create_ticket(call: ToolCall, preview: Preview, assessment: Assessment) -> tuple[str, str]:
    identifier = f"ticket_{uuid4().hex}"
    call.connection.execute(
        "INSERT INTO escalation_tickets "
        "(tenant_id, id, invoice_id, policy_id, policy_version, status, reason_code, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)",
        (call.tenant_id, identifier, preview.invoice_id, assessment.policy_id, assessment.policy_version,
         preview.reason_code, call.principal.user_id, stamp(call.clock.instant)),
    )
    if preview.approval_id is not None:
        call.connection.execute(
            "UPDATE approval_requests SET consumed_ticket_id = ?, version = version + 1 "
            "WHERE tenant_id = ? AND id = ?",
            (identifier, call.tenant_id, preview.approval_id),
        )
    event = _creation_event(call, preview, identifier, {
        "status": "open", "policy_id": assessment.policy_id, "policy_version": assessment.policy_version,
        "approval_id": preview.approval_id,
    })
    return identifier, event


def _request_approval(
    call: ToolCall, preview: Preview, scope: WriteScope, basis: str
) -> tuple[str, str, bool]:
    sandbox = call.session.sandbox
    key = (call.tenant_id, sandbox.id, sandbox.run_id, preview.invoice_id,
           call.principal.user_id, scope.digest, basis)
    existing = call.connection.execute(
        "SELECT id, creation_audit_id FROM approval_requests WHERE tenant_id = ? AND sandbox_id = ? "
        "AND run_id = ? AND invoice_id = ? AND requested_by = ? AND scope_digest = ? "
        "AND basis_digest = ? AND status = 'pending'",
        key,
    ).fetchone()
    if existing:
        return existing["id"], existing["creation_audit_id"], True
    identifier = f"approval_{uuid4().hex}"
    expires = stamp(call.clock.instant + timedelta(days=1))
    audit = _creation_event(call, preview, identifier, {"status": "pending", "expires_at": expires})
    call.connection.execute(
        "INSERT INTO approval_requests "
        "(tenant_id, sandbox_id, run_id, invoice_id, requested_by, scope_digest, basis_digest, "
        "id, status, created_at, expires_at, creation_audit_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (*key, identifier, stamp(call.clock.instant), expires, audit),
    )
    return identifier, audit, False


def decide_approval(sandbox: RunSandbox, supervisor: Principal, approval_id: str, *, approve: bool) -> None:
    """Trusted operator path. No tool declaration exposes this decision."""
    if type(approve) is not bool:
        raise ValueError("Approval decision must be a boolean")
    with sandbox.operation(), closing(connect(sandbox.path)) as db, transaction(db, write=True):
        authorize(db, supervisor, Capability.DECIDE_APPROVAL)
        row = _approval_row(db, sandbox, supervisor.tenant_id, approval_id)
        if row["requested_by"] == supervisor.user_id:
            raise Forbidden("A requester cannot decide their own approval")
        if row["status"] != "pending" or datetime.fromisoformat(row["expires_at"]) <= sandbox.clock.instant:
            raise ToolFailure("PRECONDITION_FAILED", "Approval is no longer pending")
        target = "approved" if approve else "rejected"
        db.execute(
            "UPDATE approval_requests SET status = ?, decided_by = ?, decided_at = ?, version = version + 1 "
            "WHERE tenant_id = ? AND id = ?",
            (target, supervisor.user_id, stamp(sandbox.clock.instant), supervisor.tenant_id, approval_id),
        )
        revision = bump_revision(db, supervisor.tenant_id)
        append_event(
            db, supervisor, "approval.decide", approval_id, sandbox.clock.instant, revision,
            reason_code=target.upper(), before={"status": "pending"}, after={"status": target},
            run_id=sandbox.run_id,
        )


_TICKET_QUERY = (
    "SELECT t.id, t.invoice_id, t.policy_id, t.policy_version, t.status, t.reason_code, t.created_at, t.version, "
    "a.id AS creation_audit_id, a.call_id AS creating_call_id FROM escalation_tickets t "
    "LEFT JOIN audit_events a ON a.tenant_id = t.tenant_id AND a.action = 'ticket.create' AND a.subject_id = t.id "
)


def get_ticket(call: ToolCall, args: TicketArgs) -> TicketResult:
    row = call.connection.execute(
        _TICKET_QUERY + "WHERE t.tenant_id = ? AND t.id = ?", (call.tenant_id, args.ticket_id)
    ).fetchone()
    if row is None:
        raise NotFound("Ticket not found")
    call.evidence("ticket", row["id"], row["version"], "record", invoice_id=row["invoice_id"])
    return TicketResult(**dict(row))


def search_tickets(call: ToolCall, args: TicketSearchArgs) -> Page[TicketResult]:
    _invoice(call, args.invoice_id)
    last_id = call.seek(args)
    query = _TICKET_QUERY + "WHERE t.tenant_id = ? AND t.invoice_id = ? AND t.id > ? "
    params = [call.tenant_id, args.invoice_id, last_id]
    if args.statuses is not None:
        placeholders = ",".join("?" for _ in args.statuses)
        query += f"AND t.status IN ({placeholders}) "
        params.extend(args.statuses)
    rows = call.connection.execute(query + "ORDER BY t.id LIMIT ?", (*params, args.limit + 1)).fetchall()
    items = [TicketResult(**dict(row)) for row in rows[:args.limit]]
    next_cursor = call.cursor(args, items[-1].id) if len(rows) > args.limit else None
    call.evidence("query", args.invoice_id, 0, canonical([
        "ticket.search", args.statuses, last_id, [item.id for item in items], next_cursor is None,
    ]), invoice_id=args.invoice_id)
    return Page[TicketResult](items=items, next_cursor=next_cursor)


def get_approval(call: ToolCall, args: ApprovalArgs) -> ApprovalResult:
    row = _approval_row(call.connection, call.session.sandbox, call.tenant_id, args.approval_id)
    status = row["status"]
    if (
        status in ("pending", "approved") and row["consumed_ticket_id"] is None
        and datetime.fromisoformat(row["expires_at"]) <= call.clock.instant
    ):
        status = "expired"
    call.evidence("approval", row["id"], row["version"], "record", invoice_id=row["invoice_id"])
    return ApprovalResult(
        id=row["id"], invoice_id=row["invoice_id"], status=status, expires_at=row["expires_at"],
        basis_digest=row["basis_digest"], consumed_ticket_id=row["consumed_ticket_id"],
        version=row["version"], creation_audit_id=row["creation_audit_id"], creating_call_id=row["call_id"],
    )


ACTION_TOOLS = {
    "ticket.preview_create": ToolDefinition(PreviewArgs, PreviewResult, preview_create),
    "ticket.create": ToolDefinition(CommitArgs, Receipt, commit, Capability.CREATE_TICKET, write=True),
    "ticket.get": ToolDefinition(TicketArgs, TicketResult, get_ticket),
    "ticket.search": ToolDefinition(TicketSearchArgs, Page[TicketResult], search_tickets),
    "approval.request": ToolDefinition(CommitArgs, Receipt, commit, Capability.REQUEST_APPROVAL, write=True),
    "approval.get": ToolDefinition(ApprovalArgs, ApprovalResult, get_approval),
}
