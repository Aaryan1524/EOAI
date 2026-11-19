"""Calculate an invoice balance from its payment ledger as of one instant."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Payment:
    id: str
    kind: str
    status: str
    amount_minor: int
    currency: str
    posted_at: datetime | None = None
    reversal_target_id: str | None = None


@dataclass(frozen=True)
class Balance:
    outstanding_minor: int | None
    reason: str | None = None


def outstanding_balance(
    total_minor: int,
    currency: str,
    payments: Iterable[Payment] | None,
    as_of: datetime,
) -> Balance:
    """Return the balance or a reason the invoice needs ledger review."""
    if type(total_minor) is not int or total_minor < 0:
        return Balance(None, "INVALID_INVOICE_TOTAL")
    if not isinstance(currency, str) or not currency.strip():
        return Balance(None, "INVALID_INVOICE_CURRENCY")
    cutoff = _utc_instant(as_of)
    if cutoff is None:
        return Balance(None, "INVALID_AS_OF")
    if payments is None:
        return Balance(None, "LEDGER_UNAVAILABLE")
    if isinstance(payments, (str, bytes, bytearray, Mapping)):
        return Balance(None, "INVALID_LEDGER")
    try:
        entries = iter(payments)
    except TypeError:
        return Balance(None, "INVALID_LEDGER")

    seen_ids: set[str] = set()
    posted: dict[str, tuple[Payment, datetime]] = {}
    for payment in entries:
        reason = _invalid_entry_reason(payment, currency, seen_ids)
        if reason is not None:
            return Balance(None, reason)
        seen_ids.add(payment.id)
        if payment.status != "posted":
            continue
        instant = _utc_instant(payment.posted_at)
        if instant is None:
            return Balance(None, "INVALID_POSTED_AT")
        if instant <= cutoff:
            posted[payment.id] = (payment, instant)

    outstanding = total_minor
    reversed_ids: set[str] = set()
    for payment, instant in posted.values():
        if payment.kind != "reversal":
            outstanding -= payment.amount_minor
            continue
        target_id = payment.reversal_target_id
        if not isinstance(target_id, str) or target_id not in posted:
            return Balance(None, "INVALID_REVERSAL_TARGET")
        target, target_instant = posted[target_id]
        if target.kind == "reversal" or target_instant >= instant:
            return Balance(None, "INVALID_REVERSAL_TARGET")
        if target.amount_minor != payment.amount_minor:
            return Balance(None, "REVERSAL_AMOUNT_MISMATCH")
        if target_id in reversed_ids:
            return Balance(None, "DUPLICATE_REVERSAL")
        reversed_ids.add(target_id)
        outstanding += payment.amount_minor

    return Balance(max(0, outstanding))


def _invalid_entry_reason(payment: Payment, currency: str, seen_ids: set[str]) -> str | None:
    if not isinstance(payment, Payment):
        return "INVALID_LEDGER"
    if not isinstance(payment.id, str) or not payment.id.strip():
        return "INVALID_PAYMENT_ID"
    if payment.id in seen_ids:
        return "DUPLICATE_PAYMENT_ID"
    if not isinstance(payment.kind, str) or payment.kind not in ("payment", "credit", "reversal"):
        return "UNSUPPORTED_PAYMENT_KIND"
    if not isinstance(payment.status, str) or payment.status not in ("posted", "pending", "failed"):
        return "INVALID_PAYMENT_STATUS"
    if type(payment.amount_minor) is not int or payment.amount_minor <= 0:
        return "INVALID_PAYMENT_AMOUNT"
    if not isinstance(payment.currency, str) or payment.currency != currency:
        return "PAYMENT_CURRENCY_MISMATCH"
    if payment.kind != "reversal" and payment.reversal_target_id is not None:
        return "INVALID_REVERSAL_TARGET"
    return None


def _utc_instant(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None
