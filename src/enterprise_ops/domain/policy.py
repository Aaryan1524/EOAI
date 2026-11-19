"""Evaluate escalation eligibility from authoritative domain inputs."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import (
    Dispute,
    EscalationPolicy,
    EscalationRestrictions,
    Invoice,
)
from enterprise_ops.domain.money import Balance, Payment, outstanding_balance
from enterprise_ops.domain.terms import Contract, DueDate, resolve_due_date


@dataclass(frozen=True)
class Assessment:
    invoice_id: str
    invoice_version: int
    policy_id: str | None
    policy_version: int | None
    business_date: date
    status: Literal["eligible", "ineligible", "needs_review"]
    reason_codes: tuple[str, ...]
    balance: Balance
    terms: DueDate
    currency: str
    days_past_due: int | None
    active_dispute_ids: tuple[str, ...]
    requires_approval: bool


def evaluate_escalation(
    invoice: Invoice,
    customer_status: str,
    payments: tuple[Payment, ...] | None,
    disputes: tuple[Dispute, ...],
    contract: Contract | None,
    policy: EscalationPolicy | None,
    clock: FrozenClock,
    tenant_timezone: str,
    restrictions: EscalationRestrictions = EscalationRestrictions(),
) -> Assessment:
    business_date = clock.business_date(tenant_timezone)
    balance = outstanding_balance(invoice.total_minor, invoice.currency, payments, clock.instant)
    terms = DueDate(None, None, ())
    active_disputes = tuple(sorted(d.id for d in disputes if d.is_active(clock.instant)))
    blockers = []
    review = []

    if invoice.status != "issued":
        blockers.append("INVOICE_NOT_ISSUED")
    elif invoice.issued_at is None:
        review.append("MISSING_INVOICE_ISSUE_DATE")
    elif invoice.issued_at > clock.instant:
        blockers.append("INVOICE_ISSUED_IN_FUTURE")
    else:
        issued_on = FrozenClock(invoice.issued_at).business_date(tenant_timezone)
        terms = resolve_due_date(issued_on, clock.instant, business_date, contract)
        if terms.reason is not None:
            review.append(terms.reason)

    if balance.reason is not None:
        review.append(balance.reason)
    elif balance.outstanding_minor == 0:
        blockers.append("NO_OUTSTANDING_BALANCE")

    overdue = (
        None if terms.effective_due_date is None else (business_date - terms.effective_due_date).days
    )
    if policy is None:
        review.append("MISSING_ESCALATION_POLICY")
    else:
        if customer_status in policy.excluded_customer_statuses:
            blockers.append("CUSTOMER_STATUS_EXCLUDED")
        if invoice.status in policy.excluded_invoice_statuses or (
            invoice.status == "issued"
            and _financial_status(invoice, balance) in policy.excluded_invoice_statuses
        ):
            blockers.append("INVOICE_STATUS_EXCLUDED")
        threshold = max(policy.threshold_days, restrictions.threshold_days or 0)
        if overdue is not None and overdue <= threshold:
            blockers.append("OVERDUE_THRESHOLD_NOT_MET")

    if active_disputes and (restrictions.exclude_disputed or (policy and policy.exclude_disputed)):
        blockers.append("ACTIVE_DISPUTE")

    status: Literal["eligible", "ineligible", "needs_review"]
    if review:
        status = "needs_review"
    elif blockers:
        status = "ineligible"
    else:
        status = "eligible"

    return Assessment(
        invoice_id=invoice.id,
        invoice_version=invoice.version,
        policy_id=policy.id if policy else None,
        policy_version=policy.version if policy else None,
        business_date=business_date,
        status=status,
        reason_codes=tuple(review + blockers) or ("OVERDUE_WITH_OUTSTANDING_BALANCE",),
        balance=balance,
        terms=terms,
        currency=invoice.currency,
        days_past_due=overdue,
        active_dispute_ids=active_disputes,
        requires_approval=bool(status == "eligible" and policy and policy.requires_approval),
    )


def _financial_status(invoice: Invoice, balance: Balance) -> str:
    if balance.outstanding_minor is None:
        return "unknown"
    if balance.outstanding_minor == 0:
        return "paid"
    if balance.outstanding_minor < invoice.total_minor:
        return "partially_paid"
    return "unpaid"
