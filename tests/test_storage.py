import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import Capability, Principal
from enterprise_ops.domain.transitions import InvalidTransition
from enterprise_ops.fixtures.seed import seed_baseline
from enterprise_ops.storage.repository import (
    Forbidden,
    NotFound,
    assess_invoice,
    authorize,
    close_dispute,
    open_dispute,
    transition_invoice,
    transition_payment,
)
from enterprise_ops.storage.sqlite import connect, initialize


NORTH = Principal("northstar_property", "analyst")
SIMULATOR = Principal(NORTH.tenant_id, "simulator")
BLUE = Principal("bluewater_services", "analyst")


def _revision(db, principal=NORTH):
    return db.execute("SELECT revision FROM tenants WHERE id = ?", (principal.tenant_id,)).fetchone()[0]


def test_migration_preserves_existing_domain_state(tmp_path):
    original = Path(__file__).parents[1] / "src/enterprise_ops/storage/migrations/0001_initial.sql"
    with closing(connect(tmp_path / "existing.sqlite")) as db:
        db.executescript(original.read_text(encoding="utf-8"))
        clock = seed_baseline(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        before = assess_invoice(db, NORTH, "INV-104", clock)
        initialize(db)
        initialize(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert assess_invoice(db, NORTH, "INV-104", clock) == before
        assert db.execute("SELECT policy_version FROM escalation_tickets WHERE id = 'TCK-901'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_seed_integrity_and_no_cross_tenant_id_leak(baseline):
    db, clock = baseline
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(ValueError, match="empty database"):
        seed_baseline(db)
    assert _revision(db) == 1

    north = assess_invoice(db, NORTH, "INV-104", clock)
    blue = assess_invoice(db, BLUE, "INV-104", clock)
    assert north.balance.outstanding_minor == 1_000_000
    assert blue.balance.outstanding_minor == 12_400
    assert all(NORTH.tenant_id in ref for ref in north.terms.source_refs)
    assert all(BLUE.tenant_id in ref for ref in blue.terms.source_refs)
    for invoice_id in ("INV-B01", "absent"):
        with pytest.raises(NotFound, match="^Invoice not found$"):
            assess_invoice(db, NORTH, invoice_id, clock)


def test_role_and_active_user_checks(baseline):
    db, clock = baseline
    viewer = Principal(NORTH.tenant_id, "viewer")
    supervisor = Principal(NORTH.tenant_id, "supervisor")
    assert assess_invoice(db, viewer, "INV-104", clock).status == "eligible"
    authorize(db, NORTH, Capability.CREATE_TICKET)
    authorize(db, supervisor, Capability.DECIDE_APPROVAL)
    with pytest.raises(Forbidden):
        authorize(db, viewer, Capability.CREATE_TICKET)
    with pytest.raises(Forbidden):
        transition_invoice(db, NORTH, "INV-104", "void", clock)
    db.execute("UPDATE users SET active = 0 WHERE tenant_id = ? AND id = 'viewer'", (NORTH.tenant_id,))
    with pytest.raises(Forbidden):
        assess_invoice(db, viewer, "INV-104", clock)


def test_foreign_keys_reject_wrong_customer_contract_and_foreign_invoice(baseline):
    db, _ = baseline
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "UPDATE invoices SET contract_id = 'C-301' WHERE tenant_id = ? AND id = 'INV-104'",
            (NORTH.tenant_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "UPDATE payments SET invoice_id = 'INV-B01' WHERE tenant_id = ? AND id = 'PMT-1'",
            (NORTH.tenant_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "UPDATE payment_clauses SET document_id = 'D-ACME-SIGNED' "
            "WHERE tenant_id = ? AND contract_id = 'C-202'",
            (BLUE.tenant_id,),
        )
    db.execute(
        "UPDATE payment_clauses SET id = 'different_clause' WHERE tenant_id = ? AND contract_id = 'C-201'",
        (NORTH.tenant_id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "UPDATE amendment_changes SET clause_id = 'different_clause' "
            "WHERE tenant_id = ? AND contract_id = 'C-202' AND amendment_id = 'A-1'",
            (NORTH.tenant_id,),
        )


def test_term_sources_must_belong_to_their_contract_or_amendment(baseline):
    db, _ = baseline
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "UPDATE payment_clauses SET document_id = 'D-DECOY' "
            "WHERE tenant_id = ? AND contract_id = 'C-202'",
            (NORTH.tenant_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "UPDATE amendment_changes SET document_id = 'D-ACME-DRAFT' "
            "WHERE tenant_id = ? AND contract_id = 'C-202' AND amendment_id = 'A-1'",
            (NORTH.tenant_id,),
        )


@pytest.mark.parametrize(
    "corrupt",
    [
        "UPDATE document_versions SET kind = 'contract' WHERE id = 'D-ACME-SIGNED'",
        "UPDATE document_versions SET content = 'Changed terms' WHERE id = 'D-ACME-SIGNED'",
        "UPDATE document_spans SET end_offset = 500 WHERE document_id = 'D-ACME-SIGNED'",
    ],
)
def test_invalid_term_span_fails_closed(baseline, corrupt):
    db, clock = baseline
    db.execute(corrupt)
    result = assess_invoice(db, NORTH, "INV-104", clock)
    assert result.status == "needs_review"
    assert result.terms.reason == "MISSING_TERM_EVIDENCE"


def test_duplicate_constraint_spans_policy_versions_and_active_states(baseline):
    db, clock = baseline
    assert assess_invoice(db, NORTH, "INV-106", clock).policy_version == 2
    assert db.execute(
        "SELECT policy_version FROM escalation_tickets WHERE tenant_id = ? AND id = 'TCK-901'",
        (NORTH.tenant_id,),
    ).fetchone()[0] == 1
    insert = (
        "INSERT INTO escalation_tickets "
        "(tenant_id,id,invoice_id,policy_id,policy_version,status,reason_code,created_by,created_at) "
        "SELECT tenant_id,'second',invoice_id,policy_id,2,'in_progress',reason_code,created_by,created_at "
        "FROM escalation_tickets WHERE tenant_id = ? AND id = 'TCK-901'"
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(insert, (NORTH.tenant_id,))
    db.execute(
        "UPDATE escalation_tickets SET status = 'resolved' WHERE tenant_id = ? AND id = 'TCK-901'",
        (NORTH.tenant_id,),
    )
    db.execute(insert, (NORTH.tenant_id,))
    assert db.execute("SELECT count(*) FROM escalation_tickets").fetchone()[0] == 2


def test_invoice_transitions_and_revision(baseline):
    db, clock = baseline
    db.execute(
        "INSERT INTO invoices (tenant_id,id,customer_id,contract_id,currency,total_minor,status) "
        "VALUES (?, 'draft', 'acme', 'C-202', 'USD', 10000, 'draft')",
        (NORTH.tenant_id,),
    )
    assert assess_invoice(db, NORTH, "draft", clock).status == "ineligible"
    transition_invoice(db, SIMULATOR, "draft", "issued", clock)
    issued = assess_invoice(db, NORTH, "draft", clock)
    assert issued.invoice_version == 2
    assert issued.days_past_due == -30
    transition_invoice(db, SIMULATOR, "draft", "void", clock)
    with pytest.raises(InvalidTransition, match="Cannot change void to issued"):
        transition_invoice(db, SIMULATOR, "draft", "issued", clock)
    assert _revision(db) == 3
    assert _revision(db, BLUE) == 1


def test_payment_transition_can_fail_or_post_once(baseline):
    db, clock = baseline
    transition_payment(db, SIMULATOR, "PMT-2", "posted", clock)
    assert assess_invoice(db, NORTH, "INV-104", clock).balance.outstanding_minor == 900_000
    earlier = FrozenClock(clock.instant - timedelta(microseconds=1))
    assert assess_invoice(db, NORTH, "INV-104", earlier).balance.outstanding_minor == 1_000_000
    with pytest.raises(InvalidTransition, match="Cannot change posted to failed"):
        transition_payment(db, SIMULATOR, "PMT-2", "failed", clock)
    assert _revision(db) == 2
    with pytest.raises(NotFound, match="Payment not found"):
        transition_payment(db, Principal(BLUE.tenant_id, "simulator"), "PMT-2", "failed", clock)


def test_invalid_pending_reversal_rolls_back_and_valid_reversal_posts_once(baseline):
    db, clock = baseline
    insert = (
        "INSERT INTO payments "
        "(tenant_id,id,invoice_id,kind,status,amount_minor,currency,reversal_target_id) "
        "VALUES (?, ?, 'INV-104', 'reversal', 'pending', ?, 'USD', 'PMT-1')"
    )
    db.execute(insert, (NORTH.tenant_id, "invalid", 5))
    with pytest.raises(InvalidTransition, match="REVERSAL_AMOUNT_MISMATCH"):
        transition_payment(db, SIMULATOR, "invalid", "posted", clock)
    assert _revision(db) == 1
    transition_payment(db, SIMULATOR, "invalid", "failed", clock)
    db.execute(insert, (NORTH.tenant_id, "valid", 200_000))
    db.execute(insert, (NORTH.tenant_id, "duplicate", 200_000))
    transition_payment(db, SIMULATOR, "valid", "posted", clock)
    with pytest.raises(InvalidTransition, match="DUPLICATE_REVERSAL"):
        transition_payment(db, SIMULATOR, "duplicate", "posted", clock)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(
            "UPDATE payments SET status = 'posted', posted_at = ? WHERE tenant_id = ? AND id = 'duplicate'",
            (clock.instant.isoformat(timespec="microseconds"), NORTH.tenant_id),
        )
    assert assess_invoice(db, NORTH, "INV-104", clock).balance.outstanding_minor == 1_200_000
    assert _revision(db) == 3


def test_dispute_interval_and_terminal_state(baseline):
    db, clock = baseline
    close_time = FrozenClock(clock.instant + timedelta(hours=1))
    open_dispute(db, SIMULATOR, "INV-104", "new-dispute", "Service mismatch", clock)
    earlier = FrozenClock(clock.instant - timedelta(microseconds=1))
    assert assess_invoice(db, NORTH, "INV-104", earlier).status == "eligible"
    assert assess_invoice(db, NORTH, "INV-104", clock).active_dispute_ids == ("new-dispute",)
    with pytest.raises(InvalidTransition):
        close_dispute(db, SIMULATOR, "new-dispute", earlier)
    close_dispute(db, SIMULATOR, "new-dispute", close_time)
    assert assess_invoice(db, NORTH, "INV-104", clock).status == "ineligible"
    assert assess_invoice(db, NORTH, "INV-104", close_time).status == "eligible"
    with pytest.raises(InvalidTransition):
        close_dispute(db, SIMULATOR, "new-dispute", close_time)
    assert _revision(db) == 3


def test_future_invoice_cannot_be_escalated_or_mutated(baseline):
    db, clock = baseline
    future = "2026-12-01T15:00:00.000000+00:00"
    assert datetime.fromisoformat(future) > clock.instant
    db.execute(
        "UPDATE invoices SET issued_at = ? WHERE tenant_id = ? AND id = 'INV-104'",
        (future, NORTH.tenant_id),
    )
    result = assess_invoice(db, NORTH, "INV-104", clock)
    assert result.status == "ineligible"
    assert "INVOICE_ISSUED_IN_FUTURE" in result.reason_codes
    with pytest.raises(InvalidTransition):
        transition_invoice(db, SIMULATOR, "INV-104", "void", clock)
    with pytest.raises(InvalidTransition):
        transition_payment(db, SIMULATOR, "PMT-2", "posted", clock)
    assert _revision(db) == 1
