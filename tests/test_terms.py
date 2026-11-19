from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from enterprise_ops.domain.terms import (
    Amendment,
    AmendmentChange,
    Contract,
    DueDate,
    PaymentClause,
    resolve_due_date,
)

ISSUED_ON = date(2026, 9, 1)
AS_OF = datetime(2026, 11, 19, 17, tzinfo=UTC)
BUSINESS_DATE = AS_OF.date()
BASE = PaymentClause("payment", 30, None, None, "base")
CONTRACT = Contract(
    "contract",
    "signed",
    datetime(2026, 7, 1, tzinfo=UTC),
    date(2026, 8, 1),
    date(2026, 10, 1),
    (BASE,),
)


def amendment(
    ref: str | None,
    value: int | bool,
    priority: int | None = 1,
    *,
    field: str = "net_days",
    clause_id: str = BASE.id,
) -> Amendment:
    return Amendment(
        f"amendment-{ref}",
        "signed",
        datetime(2026, 8, 29, tzinfo=UTC),
        ISSUED_ON,
        None,
        None,
        priority,
        (AmendmentChange(clause_id, field, value, ref),),
    )


def resolve(
    contract: Contract | None = CONTRACT, issued_on: date = ISSUED_ON
) -> DueDate:
    return resolve_due_date(issued_on, AS_OF, BUSINESS_DATE, contract)


def test_optional_adjustments_follow_calendar_day_arithmetic():
    contract = replace(CONTRACT, clauses=(replace(BASE, grace_days=2, next_weekday=True),))
    assert resolve(contract) == DueDate(date(2026, 10, 5), None, ("base",))
    assert resolve() == DueDate(date(2026, 10, 1), None, ("base",))


def test_terms_handle_leap_day():
    contract = replace(
        CONTRACT,
        signed_at=datetime(2024, 1, 1, tzinfo=UTC),
        issue_from=date(2024, 1, 1),
        clauses=(replace(BASE, net_days=1),),
    )
    assert resolve(contract, date(2024, 2, 28)).effective_due_date == date(2024, 2, 29)


def test_contract_coverage_is_half_open_and_terminated_contracts_apply_historically():
    contract = replace(CONTRACT, status="terminated")
    assert resolve(contract, CONTRACT.issue_from).effective_due_date == date(2026, 8, 31)
    assert resolve(contract, date(2026, 9, 30)).reason is None
    for outside in (date(2026, 7, 31), date(2026, 10, 1)):
        assert resolve(contract, outside).reason == "CONTRACT_NOT_APPLICABLE"
    assert resolve(replace(contract, issue_to=None)).reason == "INVALID_CONTRACT"


def test_contract_must_be_signed_at_evaluation_instant():
    simultaneous = AS_OF.astimezone(timezone(timedelta(hours=-5)))
    assert resolve(replace(CONTRACT, signed_at=simultaneous)).reason is None
    assert resolve(replace(CONTRACT, signed_at=AS_OF + timedelta(seconds=1))).reason == (
        "CONTRACT_NOT_APPLICABLE"
    )
    assert resolve(replace(CONTRACT, status="draft", signed_at=None)).reason == (
        "CONTRACT_NOT_APPLICABLE"
    )
    assert resolve(None).reason == "MISSING_CONTRACT"


def test_amendment_defaults_to_activation_and_can_explicitly_reach_earlier_issues():
    active_later = replace(
        amendment("revised", 20),
        activation_on=date(2026, 9, 2),
        issue_to=date(2026, 9, 3),
    )
    contract = replace(CONTRACT, amendments=(active_later,))
    assert resolve(contract) == DueDate(date(2026, 10, 1), None, ("base",))
    assert resolve(contract, date(2026, 9, 2)) == DueDate(
        date(2026, 9, 22), None, ("base", "revised")
    )
    assert resolve(contract, date(2026, 9, 3)) == DueDate(
        date(2026, 10, 3), None, ("base",)
    )
    retroactive = replace(active_later, issue_from=ISSUED_ON)
    assert resolve(replace(CONTRACT, amendments=(retroactive,))) == DueDate(
        date(2026, 9, 21), None, ("base", "revised")
    )


def test_amendment_signature_and_business_activation_are_independent_boundaries():
    active = replace(
        amendment("revised", 20),
        signed_at=AS_OF,
        activation_on=BUSINESS_DATE,
        issue_from=ISSUED_ON,
    )
    assert resolve(replace(CONTRACT, amendments=(active,))) == DueDate(
        date(2026, 9, 21), None, ("base", "revised")
    )
    for inactive in (
        replace(active, signed_at=AS_OF + timedelta(seconds=1)),
        replace(active, activation_on=BUSINESS_DATE + timedelta(days=1)),
        replace(active, status="draft", signed_at=None),
        amendment("unrelated", 20, clause_id="other"),
    ):
        assert resolve(replace(CONTRACT, amendments=(inactive,))) == DueDate(
            date(2026, 10, 1), None, ("base",)
        )


def test_highest_explicit_priority_is_applied_per_field():
    revised = amendment("winning-net", 30, 5)
    newer = replace(amendment(None, 12, 1), signed_at=AS_OF)
    grace = amendment("winning-grace", 2, 1, field="grace_days")
    weekday = amendment("winning-weekday", True, 1, field="next_weekday")
    result = resolve(replace(CONTRACT, amendments=(newer, weekday, grace, revised)))
    assert result == DueDate(
        date(2026, 10, 5), None, ("base", "winning-net", "winning-grace", "winning-weekday")
    )


@pytest.mark.parametrize("priorities", [(1, 1), (None, 1), (None, None)])
def test_conflicting_amendments_include_both_sources_for_review(priorities):
    first = amendment("span-a", 10, priorities[0])
    second = amendment("span-b", 20, priorities[1])
    assert resolve(replace(CONTRACT, amendments=(second, first))) == DueDate(
        None, "AMBIGUOUS_AMENDMENT", ("base", "span-a", "span-b")
    )


@pytest.mark.parametrize("priorities", [(1, 1), (None, 1), (None, None)])
def test_agreeing_amendments_can_share_evidence(priorities):
    first = amendment("span-a", 20, priorities[0])
    second = amendment("span-b", 20, priorities[1])
    assert resolve(replace(CONTRACT, amendments=(first, second))) == DueDate(
        date(2026, 9, 21), None, ("base", "span-a", "span-b")
    )


def test_missing_base_and_winning_amendment_evidence_require_review():
    revised = amendment("winner", 20)
    contract = replace(CONTRACT, clauses=(replace(BASE, source_ref=None),), amendments=(revised,))
    assert resolve(contract) == DueDate(None, "MISSING_TERM_EVIDENCE", ())
    amendments = (amendment("loser", 10, 0), amendment(None, 20))
    assert resolve(replace(CONTRACT, amendments=amendments)) == DueDate(
        None, "MISSING_TERM_EVIDENCE", ("base",)
    )


@pytest.mark.parametrize(
    ("clauses", "reason"),
    [
        ((), "UNSUPPORTED_PAYMENT_CLAUSE"),
        ((BASE, replace(BASE, id="another", source_ref="another")), "UNSUPPORTED_PAYMENT_CLAUSE"),
        ((replace(BASE, rule="business_days"),), "UNSUPPORTED_PAYMENT_CLAUSE"),
        ((replace(BASE, net_days=None),), "INVALID_PAYMENT_TERMS"),
        ((replace(BASE, net_days=-1),), "INVALID_PAYMENT_TERMS"),
        ((replace(BASE, net_days=10**10),), "DUE_DATE_OVERFLOW"),
    ],
)
def test_unsupported_or_invalid_term_data_requires_review(clauses, reason):
    assert resolve(replace(CONTRACT, clauses=clauses)).reason == reason


def test_unsupported_or_invalid_amendment_data_requires_review():
    unsupported = amendment("unsupported", 20, field="holiday_adjustment")
    assert resolve(replace(CONTRACT, amendments=(unsupported,))) == DueDate(
        None, "UNSUPPORTED_AMENDMENT_FIELD", ("base", "unsupported")
    )
    invalid = amendment("invalid", True)
    assert resolve(replace(CONTRACT, amendments=(invalid,))) == DueDate(
        None, "INVALID_PAYMENT_TERMS", ("base", "invalid")
    )
