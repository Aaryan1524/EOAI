"""Resolve contractual payment terms from signed, evidenced clauses."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TypeGuard


@dataclass(frozen=True, slots=True)
class PaymentClause:
    id: str
    net_days: int | None
    grace_days: int | None
    next_weekday: bool | None
    source_ref: str | None
    rule: str = "calendar"


@dataclass(frozen=True, slots=True)
class AmendmentChange:
    clause_id: str
    field: str
    value: int | bool
    source_ref: str | None


@dataclass(frozen=True, slots=True)
class Amendment:
    id: str
    status: str
    signed_at: datetime | None
    activation_on: date | None
    issue_from: date | None
    issue_to: date | None
    priority: int | None
    changes: tuple[AmendmentChange, ...]


@dataclass(frozen=True, slots=True)
class Contract:
    id: str
    status: str
    signed_at: datetime | None
    issue_from: date
    issue_to: date | None
    clauses: tuple[PaymentClause, ...]
    amendments: tuple[Amendment, ...] = ()


@dataclass(frozen=True, slots=True)
class DueDate:
    effective_due_date: date | None
    reason: str | None
    source_refs: tuple[str, ...]


class _UnresolvedTerms(ValueError):
    pass


def _utc(instant: datetime | None, reason: str) -> datetime:
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise _UnresolvedTerms(reason)
    try:
        return instant.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise _UnresolvedTerms(reason) from error


def _covers(issued_on: date, start: date, end: date | None, reason: str) -> bool:
    if type(start) is not date:
        raise _UnresolvedTerms(reason)
    if end is not None and (type(end) is not date or end <= start):
        raise _UnresolvedTerms(reason)
    return start <= issued_on and (end is None or issued_on < end)


def _contract_applies(contract: Contract, issued_on: date, as_of: datetime) -> bool:
    if contract.status == "draft":
        return False
    if contract.status not in ("signed", "terminated"):
        raise _UnresolvedTerms("INVALID_CONTRACT")
    if contract.status == "terminated" and contract.issue_to is None:
        raise _UnresolvedTerms("INVALID_CONTRACT")
    return _utc(contract.signed_at, "INVALID_CONTRACT") <= as_of and _covers(
        issued_on, contract.issue_from, contract.issue_to, "INVALID_CONTRACT"
    )


def _amendment_applies(
    amendment: Amendment, issued_on: date, as_of: datetime, business_date: date
) -> bool:
    if amendment.status == "draft":
        return False
    if amendment.status != "signed":
        raise _UnresolvedTerms("INVALID_AMENDMENT")
    if _utc(amendment.signed_at, "INVALID_AMENDMENT") > as_of:
        return False

    activation = amendment.activation_on
    if type(activation) is not date:
        raise _UnresolvedTerms("INVALID_AMENDMENT")
    if activation > business_date:
        return False
    start = amendment.issue_from if amendment.issue_from is not None else activation
    return _covers(issued_on, start, amendment.issue_to, "INVALID_AMENDMENT")


def _validate_value(field: str, value: int | bool | None) -> None:
    if field == "next_weekday":
        if type(value) is not bool:
            raise _UnresolvedTerms("INVALID_PAYMENT_TERMS")
    elif type(value) is not int or value < 0:
        raise _UnresolvedTerms("INVALID_PAYMENT_TERMS")


def _has_evidence(ref: str | None) -> TypeGuard[str]:
    return isinstance(ref, str) and bool(ref.strip())


def _select_value(
    field: str,
    candidates: list[tuple[int | None, AmendmentChange]],
    refs: dict[str, None],
) -> int | bool:
    highest = max((priority for priority, _ in candidates if priority is not None), default=None)
    # An unranked change could affect the result, so it must agree with the best ranked change.
    contenders = [change for priority, change in candidates if priority in (None, highest)]
    contender_refs = sorted(
        change.source_ref for change in contenders if _has_evidence(change.source_ref)
    )
    refs.update(dict.fromkeys(contender_refs))
    for change in contenders:
        _validate_value(field, change.value)
    values = {change.value for change in contenders}
    if len(values) > 1:
        raise _UnresolvedTerms("AMBIGUOUS_AMENDMENT")
    if any(not _has_evidence(change.source_ref) for change in contenders):
        raise _UnresolvedTerms("MISSING_TERM_EVIDENCE")
    return values.pop()


def resolve_due_date(
    issued_on: date, as_of: datetime, business_date: date, contract: Contract | None
) -> DueDate:
    """Return the effective due date or a review reason with available evidence."""
    refs: dict[str, None] = {}
    try:
        due = _resolve(issued_on, as_of, business_date, contract, refs)
    except _UnresolvedTerms as error:
        return DueDate(None, str(error), tuple(refs))
    except OverflowError:
        return DueDate(None, "DUE_DATE_OVERFLOW", tuple(refs))
    return DueDate(due, None, tuple(refs))


def _resolve(
    issued_on: date,
    as_of: datetime,
    business_date: date,
    contract: Contract | None,
    refs: dict[str, None],
) -> date:
    if type(issued_on) is not date or type(business_date) is not date:
        raise _UnresolvedTerms("INVALID_EVALUATION_DATE")
    as_of = _utc(as_of, "INVALID_EVALUATION_DATE")
    if contract is None:
        raise _UnresolvedTerms("MISSING_CONTRACT")
    if not _contract_applies(contract, issued_on, as_of):
        raise _UnresolvedTerms("CONTRACT_NOT_APPLICABLE")

    refs.update(dict.fromkeys(
        clause.source_ref for clause in contract.clauses if _has_evidence(clause.source_ref)
    ))
    if len(contract.clauses) != 1 or contract.clauses[0].rule != "calendar":
        raise _UnresolvedTerms("UNSUPPORTED_PAYMENT_CLAUSE")
    (clause,) = contract.clauses
    if not _has_evidence(clause.source_ref):
        raise _UnresolvedTerms("MISSING_TERM_EVIDENCE")

    if clause.net_days is None:
        raise _UnresolvedTerms("INVALID_PAYMENT_TERMS")
    values = {
        "net_days": clause.net_days,
        "grace_days": 0 if clause.grace_days is None else clause.grace_days,
        "next_weekday": False if clause.next_weekday is None else clause.next_weekday,
    }
    for field, value in values.items():
        _validate_value(field, value)

    candidates: dict[str, list[tuple[int | None, AmendmentChange]]] = {
        field: [] for field in values
    }
    for amendment in contract.amendments:
        changes = [change for change in amendment.changes if change.clause_id == clause.id]
        if not changes or not _amendment_applies(amendment, issued_on, as_of, business_date):
            continue
        if amendment.priority is not None and type(amendment.priority) is not int:
            raise _UnresolvedTerms("INVALID_AMENDMENT")
        for change in changes:
            if change.field not in candidates:
                if _has_evidence(change.source_ref):
                    refs[change.source_ref] = None
                raise _UnresolvedTerms("UNSUPPORTED_AMENDMENT_FIELD")
            candidates[change.field].append((amendment.priority, change))

    for field, changes in candidates.items():
        if changes:
            values[field] = _select_value(field, changes, refs)

    due = issued_on + timedelta(days=values["net_days"] + values["grace_days"])
    if values["next_weekday"] and due.weekday() >= 5:
        due += timedelta(days=7 - due.weekday())
    return due
