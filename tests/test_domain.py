from datetime import date, datetime

import pytest

from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import EscalationRestrictions, Principal
from enterprise_ops.storage.repository import assess_invoice


NORTH = Principal("northstar_property", "analyst")
BLUE = Principal("bluewater_services", "analyst")


def test_acme_baseline(baseline):
    db, clock = baseline
    result = assess_invoice(db, NORTH, "INV-104", clock)
    assert result.status == "eligible"
    assert result.balance.outstanding_minor == 1_000_000
    assert result.currency == "USD"
    assert result.terms.effective_due_date == date(2026, 10, 1)
    assert result.days_past_due == 49
    assert any("D-ACME-SIGNED" in ref for ref in result.terms.source_refs)
    assert all("D-ACME-DRAFT" not in ref for ref in result.terms.source_refs)


@pytest.mark.parametrize(
    ("invoice_id", "status", "reason"),
    [
        ("INV-105", "ineligible", "ACTIVE_DISPUTE"),
        ("INV-107", "ineligible", "NO_OUTSTANDING_BALANCE"),
        ("INV-M01", "needs_review", "AMBIGUOUS_AMENDMENT"),
        ("INV-M02", "needs_review", "MISSING_TERM_EVIDENCE"),
    ],
)
def test_baseline_exclusions_and_ambiguities(baseline, invoice_id, status, reason):
    db, clock = baseline
    result = assess_invoice(db, NORTH, invoice_id, clock)
    assert result.status == status
    assert reason in result.reason_codes
    if invoice_id == "INV-M01":
        assert any("D-MERIDIAN-A" in ref for ref in result.terms.source_refs)
        assert any("D-MERIDIAN-B" in ref for ref in result.terms.source_refs)


@pytest.mark.parametrize(
    ("instant", "days", "status"),
    [
        ("2026-11-01T03:59:59+00:00", 30, "ineligible"),
        ("2026-11-01T04:00:00+00:00", 31, "eligible"),
    ],
)
def test_threshold_uses_local_date_and_excludes_day_thirty(baseline, instant, days, status):
    db, _ = baseline
    result = assess_invoice(db, NORTH, "INV-104", FrozenClock(datetime.fromisoformat(instant)))
    assert result.days_past_due == days
    assert result.status == status


def test_request_can_tighten_but_cannot_lower_tenant_threshold(baseline):
    db, clock = baseline
    stricter = EscalationRestrictions(threshold_days=49)
    assert assess_invoice(db, NORTH, "INV-104", clock, stricter).status == "ineligible"

    day_thirty = FrozenClock(datetime.fromisoformat("2026-10-31T15:00:00+00:00"))
    looser = EscalationRestrictions(threshold_days=10)
    assert assess_invoice(db, NORTH, "INV-104", day_thirty, looser).status == "ineligible"


def test_policy_can_allow_disputed_invoice_but_request_can_exclude_it(baseline):
    db, clock = baseline
    db.execute(
        "INSERT INTO disputes (tenant_id,id,invoice_id,opened_at,reason) VALUES (?, ?, ?, ?, ?)",
        (BLUE.tenant_id, "B-DSP", "INV-104", "2026-11-01T16:00:00.000000+00:00", "Test dispute"),
    )
    unrestricted = assess_invoice(db, BLUE, "INV-104", clock)
    restricted = assess_invoice(db, BLUE, "INV-104", clock, EscalationRestrictions(exclude_disputed=True))
    assert unrestricted.status == "eligible"
    assert unrestricted.requires_approval
    assert restricted.status == "ineligible"
    assert not restricted.requires_approval
    assert "ACTIVE_DISPUTE" in restricted.reason_codes


def test_customer_exclusion_and_missing_default_policy(baseline):
    db, clock = baseline
    db.execute("UPDATE customers SET status = 'on_hold' WHERE tenant_id = ? AND id = 'acme'", (NORTH.tenant_id,))
    assert "CUSTOMER_STATUS_EXCLUDED" in assess_invoice(db, NORTH, "INV-104", clock).reason_codes
    db.execute(
        "UPDATE tenants SET default_policy_id = NULL, default_policy_version = NULL WHERE id = ?",
        (NORTH.tenant_id,),
    )
    result = assess_invoice(db, NORTH, "INV-104", clock)
    assert result.status == "needs_review"
    assert "MISSING_ESCALATION_POLICY" in result.reason_codes


def test_due_dates_follow_the_local_issue_date(baseline):
    db, clock = baseline
    db.execute(
        "UPDATE invoices SET issued_at = ? WHERE tenant_id = ? AND id = 'INV-104'",
        ("2026-08-18T01:00:00.000000+00:00", NORTH.tenant_id),
    )
    assert assess_invoice(db, NORTH, "INV-104", clock).terms.effective_due_date == date(2026, 10, 1)


def test_clock_requires_timezone():
    with pytest.raises(ValueError, match="timezone"):
        FrozenClock(datetime(2026, 11, 19, 20))


@pytest.mark.parametrize("threshold", [True, -1, 1.5])
def test_restriction_threshold_is_a_nonnegative_integer(threshold):
    with pytest.raises(ValueError, match="nonnegative integer"):
        EscalationRestrictions(threshold_days=threshold)
