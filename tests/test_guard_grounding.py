import json

import pytest

from enterprise_ops.tools.gateway import declarations_for_profile
from experiments.guard.grounding import compact_declarations, validate_rule


@pytest.fixture
def v1():
    return declarations_for_profile("enterprise_ops:v1")


@pytest.fixture
def v2():
    return declarations_for_profile("enterprise_ops:v2")


def test_compact_declarations_preserve_profile_contract_and_response_fields(v2):
    compact = compact_declarations(v2)
    invoice_search = next(tool for tool in compact if tool["name"] == "invoice.search")
    assert invoice_search["schema_version"] == "v2"
    assert invoice_search["arguments"]["required"] == ["account_ref"]
    assert invoice_search["arguments"]["properties"]["page_size"]["maximum"] == 25
    assert "next_cursor" in invoice_search["result_fields"]
    assert len(json.dumps(compact)) < len(json.dumps(v2)) / 2
    assert "title" in next(tool for tool in v2 if tool["name"] == "invoice.search")["arguments"]
    assert validate_rule('Use invoice.search with {"page_size": 3}.', compact) is None


@pytest.mark.parametrize("text", [
    "If the current page returns a continuation cursor, fetch the next page before reporting coverage.",
    "Use customer.search and invoice.assess before reporting invoice eligibility.",
    "Call clock.now() before evaluating the due date.",
    "Call invoice.assess(invoice_id=observed_invoice_id).",
    "Call invoice.search(account_ref=customer_id, page_size=3, page_cursor=next_cursor).",
    'Use `invoice.search` with `{"page_size": 3, "statuses": ["issued"]}`.',
    'Repeat `invoice.search` with {"page_cursor": null} only for a new account.',
    "Repeat invoice.search with page_size=3 and page_cursor=next_cursor.",
])
def test_recognizes_runtime_rules_and_valid_declared_arguments(text, v2):
    assert validate_rule(text, v2) is None


@pytest.mark.parametrize("text", [
    "Call invoice.recalculate with the current ID.",
    "Call invented.compare() with the current invoice.",
    "Use `invented.compare` on each observed invoice.",
    "Call calculate(invoice_id=invoice_id).",
    'Call invoice.assess with {"due_date_threshold": "30 days"}.',
    "Invoke invoice.assess with due_date_threshold=30.",
    "Repeat invoice.search with page_size=3 and due_date_threshold=30.",
    'Call invoice.search({"page_size": 3, "account_ref": null}).',
    "Run invoice.assess(invoice_id=invoice_id, contractual_basis=observed_basis).",
    "Run invoice.assess().",
    'Use {"name": "invoice.search", "arguments": {"contractual_basis": 3}}.',
    'Use invoice.search with {"page_size": 1, "page_size": 2}.',
    "Use invoice.search(account_ref=customer_id, unknown=True).",
    "Use invoice.search(account_ref=customer_id, statuses=past_due).",
    "Use invoice.search(account_ref=customer_id, page_size=True).",
    'Use invoice.search with {"page_size": 0}.',
    'Use invoice.search with {"page_size": 3.0}.',
    'Use invoice.search with {"statuses": ["past_due"]}.',
    'Use invoice.search with {"statuses": []}.',
    'Use invoice.search with {"page_size": "3"}.',
    'Use customer.search({"query": "example", "bad_key": 3}).',
    "Run invoice.assess(invoice_id=unsafe_method()).",
    'Use invoice.search with {"account_ref": "hardcoded"} even if another account is requested.',
    "Run invoice.assess(invoice_id='a literal identifier').",
    'Use customer.search with {"query": "Acm\\u0065 Property Group"}.',
])
def test_rejects_unavailable_tools_and_argument_literals(text, v2):
    assert validate_rule(text, v2, forbidden=("Acme Property Group",)) is not None


def test_structured_references_must_match_every_active_contract(v1, v2):
    v1_rule = 'Use invoice.search with {"limit": 3}.'
    v2_rule = 'Use invoice.search with {"page_size": 3}.'
    assert validate_rule(v1_rule, v1) is None
    assert validate_rule(v2_rule, v2) is None
    assert validate_rule(v1_rule, v2) is not None
    assert validate_rule(v2_rule, v1) is not None
    assert validate_rule(v1_rule, v1 + v2) is not None
    assert validate_rule(v2_rule, v1 + v2) is not None
    assert validate_rule('Use invoice.search with {"statuses": ["issued"]}.', v1 + v2) is None


@pytest.mark.parametrize("text", [
    "Retry if the reviewer failed to return a valid verdict.",
    "Fulfill criterion_id: req_2.",
    "Complete REQUIRED_ACTION_COMPLETION before reporting success.",
    "Apply the procedure during ACME-ACCOUNT-ESCALATION.",
    "Prioritize the invoice INV-104.",
    "Read D-ACME-SIGNED.",
    "Prioritize the account Acme.",
    "Open the receipt call_0123456789abcdef.",
])
def test_rejects_evaluation_feedback_and_case_ids_in_rule_text(text, v1):
    assert validate_rule(
        text, v1, forbidden=("acme-account-escalation", "required_action_completion", "acme"),
    ) is not None


def test_reject_named_invented_argument_in_prose():
    assert validate_rule(
        "Call invoice.assess with the `due_date_threshold` parameter.",
        declarations_for_profile("enterprise_ops:v1"),
    )
