from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from enterprise_ops.domain.money import Balance, Payment, outstanding_balance


CUTOFF = datetime(2026, 11, 19, 17, tzinfo=timezone.utc)
BEFORE_CUTOFF = CUTOFF - timedelta(microseconds=1)
AFTER_CUTOFF = CUTOFF + timedelta(microseconds=1)
PAYMENT = Payment("p1", "payment", "posted", 2_000, "USD", BEFORE_CUTOFF)
REVERSAL = Payment("r1", "reversal", "posted", 2_000, "USD", CUTOFF, "p1")


def test_counts_posted_payments_and_credits_through_the_cutoff():
    ledger = [
        PAYMENT,
        Payment("c1", "credit", "posted", 500, "USD", CUTOFF),
        Payment("p2", "payment", "posted", 400, "USD", AFTER_CUTOFF),
        Payment("p3", "payment", "pending", 300, "USD"),
        Payment("p4", "payment", "failed", 100, "USD"),
        replace(REVERSAL, posted_at=AFTER_CUTOFF, reversal_target_id="unknown"),
    ]

    assert outstanding_balance(12_000, "USD", iter(ledger), CUTOFF) == Balance(9_500)


def test_uses_instants_across_the_repeated_hour_after_daylight_saving_time():
    local_zone = ZoneInfo("America/New_York")
    first_occurrence = datetime(2026, 11, 1, 1, 30, tzinfo=local_zone)
    second_occurrence = first_occurrence.replace(fold=1)
    payment = replace(PAYMENT, posted_at=first_occurrence)
    reversal = replace(REVERSAL, posted_at=second_occurrence)

    assert outstanding_balance(3_000, "USD", [reversal, payment], first_occurrence) == Balance(1_000)
    assert outstanding_balance(3_000, "USD", [reversal, payment], second_occurrence) == Balance(3_000)


def test_empty_ledger_preserves_zero_and_arbitrary_precision_minor_units():
    assert outstanding_balance(0, "USD", [], CUTOFF) == Balance(0)
    assert outstanding_balance(10**30 + 1, "USD", [], CUTOFF) == Balance(10**30 + 1)


def test_clamps_overpayment_after_applying_full_reversals():
    assert outstanding_balance(1_000, "USD", [PAYMENT], CUTOFF) == Balance(0)
    assert outstanding_balance(1_000, "USD", [REVERSAL, PAYMENT], CUTOFF) == Balance(1_000)
    credit = replace(PAYMENT, kind="credit")
    assert outstanding_balance(1_000, "USD", [REVERSAL, credit], CUTOFF) == Balance(1_000)


@pytest.mark.parametrize("total", [-1, 10.0, True, None])
def test_invalid_invoice_totals_require_review(total):
    assert outstanding_balance(total, "USD", [], CUTOFF) == Balance(None, "INVALID_INVOICE_TOTAL")


@pytest.mark.parametrize("as_of", [None, "2026-11-19", CUTOFF.replace(tzinfo=None)])
def test_cutoff_must_be_an_aware_datetime(as_of):
    assert outstanding_balance(1_000, "USD", [], as_of) == Balance(None, "INVALID_AS_OF")


def test_invoice_currency_cannot_be_empty():
    assert outstanding_balance(1_000, "", [], CUTOFF) == Balance(None, "INVALID_INVOICE_CURRENCY")


def test_missing_ledger_requires_review():
    assert outstanding_balance(1_000, "USD", None, CUTOFF) == Balance(None, "LEDGER_UNAVAILABLE")


@pytest.mark.parametrize("ledger", [0, "", {}, [None], [PAYMENT, {"id": "p2"}]])
def test_malformed_ledgers_require_review(ledger):
    assert outstanding_balance(1_000, "USD", ledger, CUTOFF) == Balance(None, "INVALID_LEDGER")


@pytest.mark.parametrize(
    ("payment", "reason"),
    [
        (replace(PAYMENT, id=" "), "INVALID_PAYMENT_ID"),
        (replace(PAYMENT, amount_minor=0), "INVALID_PAYMENT_AMOUNT"),
        (replace(PAYMENT, amount_minor=-1), "INVALID_PAYMENT_AMOUNT"),
        (replace(PAYMENT, amount_minor=True), "INVALID_PAYMENT_AMOUNT"),
        (replace(PAYMENT, amount_minor=1.0), "INVALID_PAYMENT_AMOUNT"),
        (replace(PAYMENT, status="unknown"), "INVALID_PAYMENT_STATUS"),
        (replace(PAYMENT, kind="adjustment"), "UNSUPPORTED_PAYMENT_KIND"),
        (replace(PAYMENT, currency="EUR"), "PAYMENT_CURRENCY_MISMATCH"),
        (replace(PAYMENT, posted_at=None), "INVALID_POSTED_AT"),
        (replace(PAYMENT, posted_at="2026-11-19"), "INVALID_POSTED_AT"),
        (replace(PAYMENT, posted_at=CUTOFF.replace(tzinfo=None)), "INVALID_POSTED_AT"),
        (replace(PAYMENT, reversal_target_id="p2"), "INVALID_REVERSAL_TARGET"),
    ],
)
def test_malformed_payment_requires_review(payment, reason):
    assert outstanding_balance(12_000, "USD", [payment], CUTOFF) == Balance(None, reason)


def test_duplicate_ids_across_statuses_require_review():
    ledger = [PAYMENT, replace(PAYMENT, status="pending", posted_at=None)]

    assert outstanding_balance(12_000, "USD", ledger, CUTOFF) == Balance(None, "DUPLICATE_PAYMENT_ID")


@pytest.mark.parametrize(
    "ledger",
    [
        [REVERSAL],
        [PAYMENT, replace(REVERSAL, reversal_target_id=None)],
        [PAYMENT, replace(REVERSAL, reversal_target_id=REVERSAL.id)],
        [replace(PAYMENT, status="pending"), REVERSAL],
        [replace(PAYMENT, status="failed"), REVERSAL],
        [replace(PAYMENT, posted_at=AFTER_CUTOFF), REVERSAL],
        [replace(PAYMENT, posted_at=CUTOFF), REVERSAL],
        [
            replace(PAYMENT, posted_at=CUTOFF - timedelta(seconds=1)),
            replace(REVERSAL, posted_at=BEFORE_CUTOFF),
            replace(REVERSAL, id="r2", reversal_target_id="r1"),
        ],
    ],
)
def test_reversal_requires_a_prior_posted_payment_or_credit(ledger):
    assert outstanding_balance(12_000, "USD", ledger, CUTOFF) == Balance(None, "INVALID_REVERSAL_TARGET")


def test_reversal_must_match_target_amount_and_currency():
    partial_reversal = replace(REVERSAL, amount_minor=1_999)
    assert outstanding_balance(12_000, "USD", [PAYMENT, partial_reversal], CUTOFF) == Balance(
        None, "REVERSAL_AMOUNT_MISMATCH"
    )
    foreign_reversal = replace(REVERSAL, currency="EUR")
    assert outstanding_balance(12_000, "USD", [PAYMENT, foreign_reversal], CUTOFF) == Balance(
        None, "PAYMENT_CURRENCY_MISMATCH"
    )


def test_payment_can_be_reversed_only_once():
    ledger = [PAYMENT, REVERSAL, replace(REVERSAL, id="r2")]

    assert outstanding_balance(12_000, "USD", ledger, CUTOFF) == Balance(None, "DUPLICATE_REVERSAL")
