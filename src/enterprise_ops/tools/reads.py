"""Public, tenant-scoped read tools and their closed payload schemas."""

import json
from collections.abc import Iterable
from datetime import datetime
from itertools import islice
from sqlite3 import Row
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from enterprise_ops.domain.entities import Dispute
from enterprise_ops.storage.repository import (
    NotFound,
    load_assessment,
    load_document_span,
    load_policy,
)
from enterprise_ops.tools.contracts import (
    Id,
    Page,
    PageArgs,
    StrictModel,
    ToolDefinition,
    ToolFailure,
    Version,
)
from enterprise_ops.tools.session import assessment_locator

MinorUnits = Annotated[int, Field(ge=0)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
IsoInstant = Annotated[str, StringConstraints(min_length=20, max_length=40)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
ContentHash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
CustomerStatus = Literal["active", "on_hold", "closed"]
InvoiceStatus = Literal["draft", "issued", "void", "cancelled"]
PolicyInvoiceStatus = Literal[
    "draft", "issued", "void", "cancelled", "paid", "partially_paid", "unpaid"
]
AssessmentStatus = Literal["eligible", "ineligible", "needs_review"]

MAX_EXCERPT_LENGTH = 8_192


class EmptyArgs(StrictModel):
    pass


class CustomerSearchArgs(PageArgs):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class CustomerArgs(StrictModel):
    customer_id: Id


class InvoiceArgs(StrictModel):
    invoice_id: Id


class InvoiceSearchArgs(PageArgs):
    customer_id: Id
    statuses: Annotated[list[InvoiceStatus], Field(min_length=1, max_length=4)] | None = None


class InvoicePageArgs(PageArgs):
    invoice_id: Id


class ContractArgs(StrictModel):
    contract_id: Id


class ContractPageArgs(PageArgs):
    contract_id: Id


class SpanReference(StrictModel):
    document_id: Id
    document_version: Version
    span_id: Id


class ClockResult(StrictModel):
    as_of: IsoInstant
    timezone: ShortText
    business_date: IsoDate


class CustomerHit(StrictModel):
    id: Id
    legal_name: ShortText
    match_kind: Literal["exact", "alias", "substring"]
    status: CustomerStatus


class CustomerResult(StrictModel):
    id: Id
    legal_name: ShortText
    status: CustomerStatus
    aliases: list[ShortText]
    version: Version


class InvoiceResult(StrictModel):
    id: Id
    customer_id: Id
    contract_id: Id | None
    issued_at: IsoInstant | None
    stated_due_date: IsoDate | None
    currency: Currency
    total_minor: MinorUnits
    status: InvoiceStatus
    version: Version


class PaymentResult(StrictModel):
    id: Id
    invoice_id: Id
    kind: Literal["payment", "credit", "reversal"]
    status: Literal["pending", "posted", "failed"]
    amount_minor: Annotated[int, Field(gt=0)]
    currency: Currency
    posted_at: IsoInstant | None
    reversal_target_id: Id | None
    version: Version


class DisputeResult(StrictModel):
    id: Id
    invoice_id: Id
    opened_at: IsoInstant
    closed_at: IsoInstant | None
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
    active: bool
    version: Version


class ClauseResult(StrictModel):
    id: Id
    rule: ShortText
    net_days: MinorUnits | None
    grace_days: MinorUnits | None
    next_weekday: bool | None
    span_ref: SpanReference | None
    version: Version


class ContractResult(StrictModel):
    id: Id
    customer_id: Id
    status: Literal["draft", "signed", "terminated"]
    signed_at: IsoInstant | None
    issue_from: IsoDate
    issue_to: IsoDate | None
    clauses: list[ClauseResult]
    version: Version


class AmendmentChangeResult(StrictModel):
    clause_id: Id
    field: Literal["net_days", "grace_days", "next_weekday"]
    value: int | bool
    span_ref: SpanReference | None


class AmendmentResult(StrictModel):
    id: Id
    contract_id: Id
    status: Literal["draft", "signed"]
    signed_at: IsoInstant | None
    activation_on: IsoDate | None
    issue_from: IsoDate | None
    issue_to: IsoDate | None
    priority: int | None
    changes: list[AmendmentChangeResult]
    version: Version


class ExcerptResult(SpanReference):
    excerpt: Annotated[str, StringConstraints(max_length=MAX_EXCERPT_LENGTH)]
    content_hash: ContentHash
    start_offset: Annotated[int, Field(ge=0)]
    end_offset: Annotated[int, Field(gt=0)]
    evidence_id: Id


class PolicyResult(StrictModel):
    id: Id
    version: Version
    threshold_days: Annotated[int, Field(ge=0)]
    exclude_disputed: bool
    requires_approval: bool
    excluded_customer_statuses: list[CustomerStatus]
    excluded_invoice_statuses: list[PolicyInvoiceStatus]


class BalanceResult(StrictModel):
    status: Literal["resolved", "needs_review"]
    outstanding_minor: MinorUnits | None
    reason: ShortText | None
    evidence_ids: list[Id]


class TermsResult(StrictModel):
    status: Literal["resolved", "needs_review", "not_evaluated"]
    reason: ShortText | None
    effective_due_date: IsoDate | None
    stated_due_date: IsoDate | None
    stated_due_date_differs: bool | None
    evidence_ids: list[Id]


class AssessmentResult(StrictModel):
    invoice_id: Id
    invoice_version: Version
    policy_id: Id | None
    policy_version: Version | None
    business_date: IsoDate
    status: AssessmentStatus
    reason_codes: list[ShortText]
    balance: BalanceResult
    terms: TermsResult
    effective_due_date: IsoDate | None
    outstanding_minor: MinorUnits | None
    currency: Currency
    days_past_due: int | None
    active_dispute_ids: list[Id]
    requires_approval: bool
    evidence_ids: list[Id]


def _record(call, query: str, record_id: str) -> Row:
    row = call.connection.execute(query, (call.tenant_id, record_id)).fetchone()
    if row is None:
        raise NotFound("Record not found")
    return row


def _page(
    call,
    args: PageArgs,
    items: list[dict[str, Any]],
    *,
    after: str | None,
    tool: str,
    subject: str,
    invoice_id: str | None = None,
) -> dict[str, Any]:
    more = len(items) > args.limit
    items = items[:args.limit]
    last_id = items[-1]["id"] if items else None
    next_cursor = call.cursor(args, last_id) if more else None
    locator = json.dumps(
        {
            "tool": tool,
            "arguments": args.model_dump(exclude={"cursor"}),
            "after": after,
            "last_id": last_id,
            "exhausted": not more,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    call.evidence("query", subject, 0, locator, invoice_id=invoice_id)
    return {"items": items, "next_cursor": next_cursor}


def _clock_now(call, args: EmptyArgs) -> ClockResult:
    return ClockResult(
        as_of=call.clock.instant.isoformat(),
        timezone=call.timezone,
        business_date=call.clock.business_date(call.timezone).isoformat(),
    )


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _customer_matches(rows: Iterable[Row], query: str) -> Iterable[dict[str, Any]]:
    for row in rows:
        legal_name = _normalize_name(row["legal_name"])
        if query == legal_name:
            match_kind = "exact"
        elif row["alias_match"]:
            match_kind = "alias"
        elif query in legal_name:
            match_kind = "substring"
        else:
            continue
        yield {
            "id": row["id"],
            "legal_name": row["legal_name"],
            "status": row["status"],
            "match_kind": match_kind,
        }


def _customer_search(call, args: CustomerSearchArgs) -> dict[str, Any]:
    after = call.seek(args)
    query = _normalize_name(args.query)
    # Normalize legal names in Python to match the Unicode casefold used to seed aliases.
    rows = call.connection.execute(
        "SELECT c.id, c.legal_name, c.status, a.normalized_alias IS NOT NULL AS alias_match "
        "FROM customers c LEFT JOIN customer_aliases a ON a.tenant_id = c.tenant_id "
        "AND a.customer_id = c.id AND a.normalized_alias = ? "
        "WHERE c.tenant_id = ? AND c.id > coalesce(?, '') ORDER BY c.id",
        (query, call.tenant_id, after),
    )
    items = list(islice(_customer_matches(rows, query), args.limit + 1))
    return _page(call, args, items, after=after, tool="customer.search", subject="customers")


def _customer_get(call, args: CustomerArgs) -> dict[str, Any]:
    row = _record(
        call, "SELECT id, legal_name, status, version FROM customers WHERE tenant_id = ? AND id = ?",
        args.customer_id,
    )
    aliases = call.connection.execute(
        "SELECT normalized_alias FROM customer_aliases WHERE tenant_id = ? AND customer_id = ? "
        "ORDER BY normalized_alias",
        (call.tenant_id, args.customer_id),
    )
    call.evidence("customer", row["id"], row["version"], "record")
    return {**dict(row), "aliases": [alias[0] for alias in aliases]}


_INVOICE_COLUMNS = "id, customer_id, contract_id, issued_at, stated_due_date, currency, total_minor, status, version"


def _invoice_search(call, args: InvoiceSearchArgs) -> dict[str, Any]:
    after = call.seek(args)
    _record(call, "SELECT id FROM customers WHERE tenant_id = ? AND id = ?", args.customer_id)
    filters = ""
    if args.statuses is not None:
        filters = "AND status IN (" + ", ".join("?" for _ in args.statuses) + ") "
    rows = call.connection.execute(
        f"SELECT {_INVOICE_COLUMNS} FROM invoices "
        "WHERE tenant_id = ? AND customer_id = ? AND id > coalesce(?, '') "
        f"{filters}ORDER BY id LIMIT ?",
        (call.tenant_id, args.customer_id, after, *(args.statuses or ()), args.limit + 1),
    )
    return _page(
        call, args, [dict(row) for row in rows],
        after=after, tool="invoice.search", subject=args.customer_id,
    )


def _invoice_get(call, args: InvoiceArgs) -> dict[str, Any]:
    row = _record(
        call, f"SELECT {_INVOICE_COLUMNS} FROM invoices WHERE tenant_id = ? AND id = ?", args.invoice_id
    )
    call.evidence("invoice", row["id"], row["version"], "record", invoice_id=args.invoice_id)
    return dict(row)


def _payment_list(call, args: InvoicePageArgs) -> dict[str, Any]:
    after = call.seek(args)
    _record(call, "SELECT id FROM invoices WHERE tenant_id = ? AND id = ?", args.invoice_id)
    rows = call.connection.execute(
        "SELECT id, invoice_id, kind, status, amount_minor, currency, posted_at, reversal_target_id, version "
        "FROM payments WHERE tenant_id = ? AND invoice_id = ? AND id > coalesce(?, '') "
        "ORDER BY id LIMIT ?",
        (call.tenant_id, args.invoice_id, after, args.limit + 1),
    )
    return _page(
        call, args, [dict(row) for row in rows],
        after=after, tool="payment.list_for_invoice", subject=args.invoice_id, invoice_id=args.invoice_id,
    )


def _dispute_list(call, args: InvoicePageArgs) -> dict[str, Any]:
    after = call.seek(args)
    _record(call, "SELECT id FROM invoices WHERE tenant_id = ? AND id = ?", args.invoice_id)
    rows = call.connection.execute(
        "SELECT id, invoice_id, opened_at, closed_at, reason, version FROM disputes "
        "WHERE tenant_id = ? AND invoice_id = ? AND id > coalesce(?, '') ORDER BY id LIMIT ?",
        (call.tenant_id, args.invoice_id, after, args.limit + 1),
    )
    items = []
    for row in rows:
        dispute = Dispute(
            row["id"],
            datetime.fromisoformat(row["opened_at"]),
            datetime.fromisoformat(row["closed_at"]) if row["closed_at"] is not None else None,
        )
        items.append({**dict(row), "active": dispute.is_active(call.clock.instant)})
    return _page(
        call, args, items,
        after=after, tool="dispute.list_for_invoice", subject=args.invoice_id, invoice_id=args.invoice_id,
    )


def _span_ref(row: Row) -> SpanReference | None:
    if row["span_id"] is None:
        return None
    return SpanReference(
        document_id=row["document_id"],
        document_version=row["document_version"],
        span_id=row["span_id"],
    )


def _contract_get(call, args: ContractArgs) -> dict[str, Any]:
    row = _record(
        call,
        "SELECT id, customer_id, status, signed_at, issue_from, issue_to, version "
        "FROM contracts WHERE tenant_id = ? AND id = ?",
        args.contract_id,
    )
    clauses = call.connection.execute(
        "SELECT id, net_days, grace_days, next_weekday, rule, document_id, document_version, "
        "span_id, version FROM payment_clauses WHERE tenant_id = ? AND contract_id = ? ORDER BY id",
        (call.tenant_id, args.contract_id),
    )
    call.evidence("contract", args.contract_id, row["version"], "record")
    return {
        **dict(row),
        "clauses": [
            ClauseResult(
                id=clause["id"],
                net_days=clause["net_days"],
                grace_days=clause["grace_days"],
                next_weekday=(
                    bool(clause["next_weekday"]) if clause["next_weekday"] is not None else None
                ),
                rule=clause["rule"],
                span_ref=_span_ref(clause),
                version=clause["version"],
            )
            for clause in clauses
        ],
    }


def _amendment_list(call, args: ContractPageArgs) -> dict[str, Any]:
    after = call.seek(args)
    _record(call, "SELECT id FROM contracts WHERE tenant_id = ? AND id = ?", args.contract_id)
    rows = call.connection.execute(
        "SELECT id, contract_id, status, signed_at, activation_on, issue_from, issue_to, priority, version "
        "FROM amendments WHERE tenant_id = ? AND contract_id = ? AND id > coalesce(?, '') "
        "ORDER BY id LIMIT ?",
        (call.tenant_id, args.contract_id, after, args.limit + 1),
    ).fetchall()

    items = []
    for row in rows:
        changes = call.connection.execute(
            "SELECT clause_id, field, value, document_id, document_version, span_id FROM amendment_changes "
            "WHERE tenant_id = ? AND contract_id = ? AND amendment_id = ? ORDER BY clause_id, field",
            (call.tenant_id, args.contract_id, row["id"]),
        )
        items.append({
            **dict(row),
            "changes": [
                AmendmentChangeResult(
                    clause_id=change["clause_id"],
                    field=change["field"],
                    value=bool(change["value"]) if change["field"] == "next_weekday" else change["value"],
                    span_ref=_span_ref(change),
                )
                for change in changes
            ],
        })
    return _page(
        call, args, items,
        after=after, tool="contract.list_amendments", subject=args.contract_id,
    )


def _document_excerpt(call, args: SpanReference) -> ExcerptResult:
    span = load_document_span(
        call.connection, call.tenant_id, args.document_id, args.document_version, args.span_id
    )
    if span is None:
        raise NotFound("Record not found")
    if len(span.excerpt) > MAX_EXCERPT_LENGTH:
        raise ToolFailure("PRECONDITION_FAILED", "The document span exceeds the excerpt limit")
    evidence_id = call.evidence("document_span", args.document_id, args.document_version, span.locator)
    return ExcerptResult(
        **args.model_dump(),
        excerpt=span.excerpt,
        content_hash=span.content_hash,
        start_offset=span.start_offset,
        end_offset=span.end_offset,
        evidence_id=evidence_id,
    )


def _policy_get(call, args: EmptyArgs) -> PolicyResult:
    policy = load_policy(call.connection, call.tenant_id)
    if policy is None:
        raise NotFound("Record not found")
    call.evidence("escalation_policy", policy.id, policy.version, "rules")
    return PolicyResult(
        id=policy.id,
        version=policy.version,
        threshold_days=policy.threshold_days,
        exclude_disputed=policy.exclude_disputed,
        requires_approval=policy.requires_approval,
        excluded_customer_statuses=sorted(policy.excluded_customer_statuses),
        excluded_invoice_statuses=sorted(policy.excluded_invoice_statuses),
    )


def _term_evidence(call, ref: str, invoice_id: str) -> str:
    tenant_id, document_id, document_version, span_id, content_hash, start, end = json.loads(ref)
    if tenant_id != call.tenant_id:
        raise ToolFailure("PRECONDITION_FAILED", "The term evidence is inconsistent")
    locator = json.dumps([span_id, content_hash, start, end], separators=(",", ":"))
    return call.evidence("document_span", document_id, document_version, locator, invoice_id=invoice_id)


def _invoice_assess(call, args: InvoiceArgs) -> AssessmentResult:
    assessment = load_assessment(
        call.connection, call.tenant_id, args.invoice_id, call.clock, call.restrictions
    )
    row = _record(
        call, "SELECT stated_due_date FROM invoices WHERE tenant_id = ? AND id = ?", args.invoice_id
    )
    decision_id = call.evidence(
        "invoice_assessment", args.invoice_id, assessment.invoice_version, assessment_locator(assessment),
        invoice_id=args.invoice_id,
    )
    term_ids = [_term_evidence(call, ref, args.invoice_id) for ref in assessment.terms.source_refs]
    stated = row["stated_due_date"]
    if stated is not None:
        term_ids.append(call.evidence(
            "invoice", args.invoice_id, assessment.invoice_version, "stated_due_date",
            invoice_id=args.invoice_id,
        ))

    effective = assessment.terms.effective_due_date
    effective_iso = effective.isoformat() if effective is not None else None
    terms_reason = assessment.terms.reason
    if effective is not None:
        terms_status = "resolved"
    elif terms_reason is not None:
        terms_status = "needs_review"
    else:
        terms_status = "not_evaluated"
        terms_reason = next(
            code for code in assessment.reason_codes
            if code in ("INVOICE_NOT_ISSUED", "INVOICE_ISSUED_IN_FUTURE", "MISSING_INVOICE_ISSUE_DATE")
        )

    return AssessmentResult(
        invoice_id=assessment.invoice_id,
        invoice_version=assessment.invoice_version,
        policy_id=assessment.policy_id,
        policy_version=assessment.policy_version,
        business_date=assessment.business_date.isoformat(),
        status=assessment.status,
        reason_codes=list(assessment.reason_codes),
        balance=BalanceResult(
            outstanding_minor=assessment.balance.outstanding_minor,
            status="needs_review" if assessment.balance.reason else "resolved",
            reason=assessment.balance.reason,
            evidence_ids=[decision_id],
        ),
        terms=TermsResult(
            status=terms_status,
            reason=terms_reason,
            effective_due_date=effective_iso,
            stated_due_date=stated,
            stated_due_date_differs=stated != effective_iso if stated and effective_iso else None,
            evidence_ids=term_ids,
        ),
        effective_due_date=effective_iso,
        outstanding_minor=assessment.balance.outstanding_minor,
        currency=assessment.currency,
        days_past_due=assessment.days_past_due,
        active_dispute_ids=list(assessment.active_dispute_ids),
        requires_approval=assessment.requires_approval,
        evidence_ids=[decision_id, *term_ids],
    )


READ_TOOLS: dict[str, ToolDefinition] = {
    "clock.now": ToolDefinition(EmptyArgs, ClockResult, _clock_now),
    "customer.search": ToolDefinition(CustomerSearchArgs, Page[CustomerHit], _customer_search),
    "customer.get": ToolDefinition(CustomerArgs, CustomerResult, _customer_get),
    "invoice.search": ToolDefinition(InvoiceSearchArgs, Page[InvoiceResult], _invoice_search),
    "invoice.get": ToolDefinition(InvoiceArgs, InvoiceResult, _invoice_get),
    "payment.list_for_invoice": ToolDefinition(InvoicePageArgs, Page[PaymentResult], _payment_list),
    "dispute.list_for_invoice": ToolDefinition(InvoicePageArgs, Page[DisputeResult], _dispute_list),
    "contract.get": ToolDefinition(ContractArgs, ContractResult, _contract_get),
    "contract.list_amendments": ToolDefinition(ContractPageArgs, Page[AmendmentResult], _amendment_list),
    "document.get_excerpt": ToolDefinition(SpanReference, ExcerptResult, _document_excerpt),
    "policy.get_escalation": ToolDefinition(EmptyArgs, PolicyResult, _policy_get),
    "invoice.assess": ToolDefinition(InvoiceArgs, AssessmentResult, _invoice_assess),
}
