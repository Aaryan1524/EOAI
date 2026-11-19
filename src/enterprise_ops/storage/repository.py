"""Tenant-scoped domain reads and guarded simulator mutations."""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime
from hashlib import sha256

from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import (
    ROLE_CAPABILITIES,
    Capability,
    Dispute,
    EscalationPolicy,
    EscalationRestrictions,
    Invoice,
    Principal,
)
from enterprise_ops.domain.money import Payment, outstanding_balance
from enterprise_ops.domain.policy import Assessment, evaluate_escalation
from enterprise_ops.domain.terms import Amendment, AmendmentChange, Contract, PaymentClause
from enterprise_ops.domain.transitions import (
    INVOICE_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    InvalidTransition,
    require_transition,
)
from enterprise_ops.storage.audit import append_event, bump_revision, stamp
from enterprise_ops.storage.sqlite import transaction


class NotFound(LookupError):
    """No matching record is visible to this principal."""


class Forbidden(PermissionError):
    """The current role cannot perform this operation."""


@dataclass(frozen=True)
class DocumentExcerpt:
    excerpt: str
    content_hash: str
    start_offset: int
    end_offset: int
    kind: str
    locator: str


def authorize(
    connection: sqlite3.Connection, principal: Principal, capability: Capability
) -> None:
    user = connection.execute(
        "SELECT role FROM users WHERE tenant_id = ? AND id = ? AND active = 1",
        (principal.tenant_id, principal.user_id),
    ).fetchone()
    if user is None or capability not in ROLE_CAPABILITIES[user["role"]]:
        raise Forbidden(f"Principal lacks {capability}")


def assess_invoice(
    connection: sqlite3.Connection,
    principal: Principal,
    invoice_id: str,
    clock: FrozenClock,
    restrictions: EscalationRestrictions = EscalationRestrictions(),
) -> Assessment:
    with transaction(connection):
        authorize(connection, principal, Capability.READ)
        return load_assessment(connection, principal.tenant_id, invoice_id, clock, restrictions)


def load_assessment(
    connection: sqlite3.Connection,
    tenant_id: str,
    invoice_id: str,
    clock: FrozenClock,
    restrictions: EscalationRestrictions = EscalationRestrictions(),
) -> Assessment:
    """Evaluate within the caller's authorized SQLite transaction."""
    row = _invoice(connection, tenant_id, invoice_id)
    timezone = connection.execute("SELECT timezone FROM tenants WHERE id = ?", (tenant_id,)).fetchone()[0]
    return evaluate_escalation(
        _invoice_entity(row),
        row["customer_status"],
        _payments(connection, tenant_id, invoice_id),
        _disputes(connection, tenant_id, invoice_id),
        _contract(connection, tenant_id, row["contract_id"]),
        load_policy(connection, tenant_id),
        clock,
        timezone,
        restrictions,
    )


def _audit_source_change(
    connection: sqlite3.Connection,
    principal: Principal,
    action: str,
    subject_id: str,
    clock: FrozenClock,
    before: dict | None,
    after: dict,
    run_id: str,
) -> None:
    revision = bump_revision(connection, principal.tenant_id)
    append_event(
        connection, principal, action, subject_id, clock.instant, revision,
        reason_code="SIMULATED_SOURCE_CHANGE", before=before, after=after, run_id=run_id,
    )


def transition_invoice(
    connection: sqlite3.Connection,
    principal: Principal,
    invoice_id: str,
    target: str,
    clock: FrozenClock,
    *,
    run_id: str = "simulator",
) -> None:
    tenant_id = principal.tenant_id
    with transaction(connection, write=True):
        authorize(connection, principal, Capability.SIMULATE_SOURCE_CHANGE)
        row = _invoice(connection, tenant_id, invoice_id)
        require_transition(INVOICE_TRANSITIONS, row["status"], target)
        issued_at = _instant(row["issued_at"])
        if issued_at is not None and issued_at > clock.instant:
            raise InvalidTransition("Cannot change a future-issued invoice")
        issued_stamp = stamp(clock.instant) if target == "issued" else row["issued_at"]
        connection.execute(
            "UPDATE invoices SET status = ?, issued_at = ?, version = version + 1 "
            "WHERE tenant_id = ? AND id = ?",
            (target, issued_stamp, tenant_id, invoice_id),
        )
        _audit_source_change(
            connection, principal, "invoice.transition", invoice_id, clock,
            {"status": row["status"], "issued_at": row["issued_at"], "version": row["version"]},
            {"status": target, "issued_at": issued_stamp, "version": row["version"] + 1}, run_id,
        )


def transition_payment(
    connection: sqlite3.Connection,
    principal: Principal,
    payment_id: str,
    target: str,
    clock: FrozenClock,
    *,
    run_id: str = "simulator",
) -> None:
    tenant_id = principal.tenant_id
    with transaction(connection, write=True):
        authorize(connection, principal, Capability.SIMULATE_SOURCE_CHANGE)
        row = connection.execute(
            "SELECT * FROM payments WHERE tenant_id = ? AND id = ?",
            (tenant_id, payment_id),
        ).fetchone()
        if row is None:
            raise NotFound("Payment not found")
        require_transition(PAYMENT_TRANSITIONS, row["status"], target)
        if target == "posted":
            invoice = _invoice(connection, tenant_id, row["invoice_id"])
            _require_issued_at(invoice, clock)
            entries = tuple(
                replace(entry, status=target, posted_at=clock.instant)
                if entry.id == payment_id
                else entry
                for entry in _payments(connection, tenant_id, invoice["id"])
            )
            balance = outstanding_balance(invoice["total_minor"], invoice["currency"], entries, clock.instant)
            if balance.reason is not None:
                raise InvalidTransition(balance.reason)
        posted_stamp = stamp(clock.instant) if target == "posted" else None
        connection.execute(
            "UPDATE payments SET status = ?, posted_at = ?, version = version + 1 "
            "WHERE tenant_id = ? AND id = ?",
            (target, posted_stamp, tenant_id, payment_id),
        )
        _audit_source_change(
            connection, principal, "payment.transition", payment_id, clock,
            {"status": row["status"], "version": row["version"]},
            {"status": target, "posted_at": posted_stamp, "version": row["version"] + 1}, run_id,
        )


def open_dispute(
    connection: sqlite3.Connection,
    principal: Principal,
    invoice_id: str,
    dispute_id: str,
    reason: str,
    clock: FrozenClock,
    *,
    run_id: str = "simulator",
) -> None:
    tenant_id = principal.tenant_id
    with transaction(connection, write=True):
        authorize(connection, principal, Capability.SIMULATE_SOURCE_CHANGE)
        _require_issued_at(_invoice(connection, tenant_id, invoice_id), clock)
        connection.execute(
            "INSERT INTO disputes (tenant_id, id, invoice_id, opened_at, reason) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, dispute_id, invoice_id, stamp(clock.instant), reason),
        )
        _audit_source_change(
            connection, principal, "dispute.open", dispute_id, clock, None,
            {"invoice_id": invoice_id, "opened_at": stamp(clock.instant), "version": 1}, run_id,
        )


def close_dispute(
    connection: sqlite3.Connection, principal: Principal, dispute_id: str, clock: FrozenClock,
    *, run_id: str = "simulator",
) -> None:
    tenant_id = principal.tenant_id
    with transaction(connection, write=True):
        authorize(connection, principal, Capability.SIMULATE_SOURCE_CHANGE)
        row = connection.execute(
            "SELECT opened_at, closed_at, version FROM disputes WHERE tenant_id = ? AND id = ?",
            (tenant_id, dispute_id),
        ).fetchone()
        if row is None:
            raise NotFound("Dispute not found")
        if row["closed_at"] is not None or datetime.fromisoformat(row["opened_at"]) > clock.instant:
            raise InvalidTransition("Dispute must be open at the simulation instant")
        connection.execute(
            "UPDATE disputes SET closed_at = ?, version = version + 1 WHERE tenant_id = ? AND id = ?",
            (stamp(clock.instant), tenant_id, dispute_id),
        )
        _audit_source_change(
            connection, principal, "dispute.close", dispute_id, clock,
            {"closed_at": None, "version": row["version"]},
            {"closed_at": stamp(clock.instant), "version": row["version"] + 1}, run_id,
        )


def _invoice(connection: sqlite3.Connection, tenant_id: str, invoice_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT i.*, c.status AS customer_status FROM invoices i JOIN customers c "
        "ON c.tenant_id = i.tenant_id AND c.id = i.customer_id WHERE i.tenant_id = ? AND i.id = ?",
        (tenant_id, invoice_id),
    ).fetchone()
    if row is None:
        raise NotFound("Invoice not found")
    return row


def _invoice_entity(row: sqlite3.Row) -> Invoice:
    return Invoice(
        row["id"], row["customer_id"], row["contract_id"], _instant(row["issued_at"]),
        row["currency"], row["total_minor"], row["status"], row["version"],
    )


def _require_issued_at(row: sqlite3.Row, clock: FrozenClock) -> None:
    if row["status"] != "issued" or datetime.fromisoformat(row["issued_at"]) > clock.instant:
        raise InvalidTransition("Invoice must be issued at the simulation instant")


def _payments(connection: sqlite3.Connection, tenant_id: str, invoice_id: str) -> tuple[Payment, ...]:
    rows = connection.execute(
        "SELECT * FROM payments WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",
        (tenant_id, invoice_id),
    )
    return tuple(
        Payment(
            row["id"], row["kind"], row["status"], row["amount_minor"], row["currency"],
            _instant(row["posted_at"]), row["reversal_target_id"],
        )
        for row in rows
    )


def _disputes(connection: sqlite3.Connection, tenant_id: str, invoice_id: str) -> tuple[Dispute, ...]:
    rows = connection.execute(
        "SELECT * FROM disputes WHERE tenant_id = ? AND invoice_id = ? ORDER BY id",
        (tenant_id, invoice_id),
    )
    return tuple(
        Dispute(row["id"], datetime.fromisoformat(row["opened_at"]), _instant(row["closed_at"]))
        for row in rows
    )


def load_policy(connection: sqlite3.Connection, tenant_id: str) -> EscalationPolicy | None:
    head = connection.execute(
        "SELECT default_policy_id, default_policy_version FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    if head is None:
        return None
    key = (tenant_id, head["default_policy_id"], head["default_policy_version"])
    row = connection.execute(
        "SELECT * FROM escalation_policies WHERE tenant_id = ? AND id = ? AND version = ?", key
    ).fetchone()
    if row is None:
        return None
    exclusions: dict[str, set[str]] = {"customer": set(), "invoice": set()}
    for entry in connection.execute(
        "SELECT subject, status FROM policy_exclusions "
        "WHERE tenant_id = ? AND policy_id = ? AND policy_version = ?", key
    ):
        exclusions[entry["subject"]].add(entry["status"])
    return EscalationPolicy(
        row["id"], row["version"], row["threshold_days"], bool(row["exclude_disputed"]),
        bool(row["requires_approval"]), frozenset(exclusions["customer"]),
        frozenset(exclusions["invoice"]),
    )


def _contract(
    connection: sqlite3.Connection, tenant_id: str, contract_id: str | None
) -> Contract | None:
    if contract_id is None:
        return None
    key = (tenant_id, contract_id)
    row = connection.execute(
        "SELECT * FROM contracts WHERE tenant_id = ? AND id = ?", key
    ).fetchone()
    if row is None:
        return None
    clauses = tuple(
        PaymentClause(
            clause["id"], clause["net_days"], clause["grace_days"],
            bool(clause["next_weekday"]) if clause["next_weekday"] is not None else None,
            _source_ref(connection, clause, "contract"), clause["rule"],
        )
        for clause in connection.execute(
            "SELECT * FROM payment_clauses WHERE tenant_id = ? AND contract_id = ? ORDER BY id", key
        )
    )
    changes: dict[str, list[AmendmentChange]] = {}
    for change in connection.execute(
        "SELECT * FROM amendment_changes WHERE tenant_id = ? AND contract_id = ? "
        "ORDER BY amendment_id, clause_id, field", key
    ):
        value = bool(change["value"]) if change["field"] == "next_weekday" else change["value"]
        changes.setdefault(change["amendment_id"], []).append(
            AmendmentChange(
                change["clause_id"], change["field"], value, _source_ref(connection, change, "amendment")
            )
        )
    amendments = tuple(
        Amendment(
            amendment["id"], amendment["status"], _instant(amendment["signed_at"]),
            _date(amendment["activation_on"]), _date(amendment["issue_from"]),
            _date(amendment["issue_to"]), amendment["priority"],
            tuple(changes.get(amendment["id"], ())),
        )
        for amendment in connection.execute(
            "SELECT * FROM amendments WHERE tenant_id = ? AND contract_id = ? ORDER BY id", key
        )
    )
    return Contract(
        row["id"], row["status"], _instant(row["signed_at"]), date.fromisoformat(row["issue_from"]),
        _date(row["issue_to"]), clauses, amendments,
    )


def _source_ref(connection: sqlite3.Connection, row: sqlite3.Row, kind: str) -> str | None:
    if row["span_id"] is None:
        return None
    key = (row["tenant_id"], row["document_id"], row["document_version"], row["span_id"])
    source = load_document_span(connection, *key)
    if source is None or source.kind != kind:
        return None
    return json.dumps(
        (*key, source.content_hash, source.start_offset, source.end_offset), separators=(",", ":")
    )


def load_document_span(
    connection: sqlite3.Connection,
    tenant_id: str,
    document_id: str,
    document_version: int,
    span_id: str,
) -> DocumentExcerpt | None:
    source = connection.execute(
        "SELECT s.start_offset, s.end_offset, d.kind, d.content, d.content_hash FROM document_spans s "
        "JOIN document_versions d ON d.tenant_id = s.tenant_id AND d.id = s.document_id "
        "AND d.version = s.document_version "
        "WHERE s.tenant_id = ? AND s.document_id = ? AND s.document_version = ? AND s.id = ?",
        (tenant_id, document_id, document_version, span_id),
    ).fetchone()
    if source is None or source["end_offset"] > len(source["content"]):
        return None
    if sha256(source["content"].encode("utf-8")).hexdigest() != source["content_hash"]:
        return None
    start, end, digest = source["start_offset"], source["end_offset"], source["content_hash"]
    return DocumentExcerpt(
        source["content"][start:end], digest, start, end, source["kind"],
        json.dumps([span_id, digest, start, end], separators=(",", ":")),
    )


def _instant(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value is not None else None
