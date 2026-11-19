from contextlib import closing
from dataclasses import replace

import pytest
from pydantic import ValidationError

from enterprise_ops.agent.output import Ledger, RunOutput
from enterprise_ops.domain.entities import Capability, Principal
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, seed_snapshot
from enterprise_ops.storage.repository import transition_payment
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools.gateway import ToolGateway
from enterprise_ops.tools.session import WriteScope

PRINCIPAL = Principal("northstar_property", "analyst")
ACCOUNT = "acme"
QUERY = "Acme Property Group"
TARGET = "INV-104"
REASON = "OVERDUE_WITH_OUTSTANDING_BALANCE"


@pytest.fixture
def sandbox(tmp_path):
    return seed_snapshot(tmp_path / "snapshots", "baseline").clone(tmp_path / "runs", "output")


def invoke(ledger, gateway, tool, *, ok=True, **args):
    response = gateway.invoke(tool, args)
    ledger.record(tool, args, response)
    assert response.ok == ok, response
    return response.data


def finish(ledger, gateway, *, selected=None, lookup=None, review_reason=None):
    return ledger.finish(
        run_id="output", task_id="test", as_of="2026-11-19T12:00:00+00:00",
        business_date="2026-11-19", lookup=lookup or gateway.observation,
        selected_customer_id=selected, review_reason=review_reason, stop_reason="model_terminal",
    )


def write_gateway(sandbox, *, invoice_ids=None, faults=None):
    scope = WriteScope(
        PRINCIPAL, "approved", ACCOUNT, frozenset({Capability.CREATE_TICKET, Capability.REQUEST_APPROVAL}),
        invoice_ids=invoice_ids,
    )
    return ToolGateway(sandbox, PRINCIPAL, write_scope=scope, faults=faults)


def preview(ledger, gateway, assessment):
    return invoke(
        ledger, gateway, "ticket.preview_create", invoice_id=assessment.invoice_id,
        reason_code=REASON, evidence_ids=assessment.evidence_ids,
    )


def command(proposal):
    return {
        "preview_token": proposal.preview_token,
        "idempotency_key": proposal.idempotency_key,
        "expected_tenant_revision": proposal.tenant_revision,
        "expected_invoice_version": proposal.invoice_version,
        "expected_policy_version": proposal.policy_version,
    }


def test_trusted_customer_query_must_exhaust_and_match_the_model_selection(sandbox):
    gateway = ToolGateway(sandbox, PRINCIPAL)
    ledger = Ledger("list_invoices", customer_query=QUERY)
    first = invoke(ledger, gateway, "customer.search", query=QUERY, limit=1)
    candidate = first.items[0].id
    invoke(ledger, gateway, "invoice.search", customer_id=candidate)
    assert finish(ledger, gateway, selected=candidate).coverage.customer_id is None

    invoke(ledger, gateway, "customer.search", query=QUERY, limit=1, cursor=first.next_cursor)
    assert finish(ledger, gateway, selected="unrelated").coverage.customer_id is None
    result = finish(ledger, gateway, selected=ACCOUNT)
    assert result.status == "resolved"
    assert result.coverage.observed_invoice_ids == ["INV-104", "INV-105", "INV-106", "INV-107"]
    assert result.coverage.invoice_search_exhausted
    assert not result.decisions
    assert RunOutput.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        RunOutput.model_validate({**result.model_dump(), "model_notes": "unsupported"})


def test_partial_filtered_and_restarted_pages_do_not_claim_full_coverage(sandbox):
    gateway = ToolGateway(sandbox, PRINCIPAL)
    ledger = Ledger("review_invoices", customer_id=ACCOUNT)
    invoke(ledger, gateway, "invoice.search", customer_id=ACCOUNT, statuses=["issued"])
    assert not finish(ledger, gateway).coverage.invoice_search_exhausted

    first = invoke(ledger, gateway, "invoice.search", customer_id=ACCOUNT, limit=2)
    invoke(ledger, gateway, "invoice.search", customer_id=ACCOUNT, limit=3, cursor=first.next_cursor, ok=False)
    result = finish(ledger, gateway, review_reason="untrusted narrative")
    assert result.coverage.observed_invoice_ids == ["INV-104", "INV-105"]
    assert result.coverage.pending_invoice_ids == ["INV-104", "INV-105"]
    assert not result.coverage.invoice_search_exhausted
    assert "untrusted" not in result.model_dump_json()

    invoke(ledger, gateway, "invoice.search", customer_id=ACCOUNT, limit=2, cursor=first.next_cursor)
    assert finish(ledger, gateway).coverage.invoice_search_exhausted
    invoke(ledger, gateway, "invoice.search", customer_id=ACCOUNT, limit=1)
    assert not finish(ledger, gateway).coverage.invoice_search_exhausted


def test_empty_continuation_preserves_cursor_chain(sandbox):
    faults = FaultPlan((FaultStep("invoice.search", 2, FaultKind.EMPTY_PAGE),))
    gateway = ToolGateway(sandbox, PRINCIPAL, faults=faults)
    ledger = Ledger("list_invoices", customer_id=ACCOUNT)
    args = {"customer_id": ACCOUNT, "limit": 2}
    first = invoke(ledger, gateway, "invoice.search", **args)
    empty = invoke(ledger, gateway, "invoice.search", **args, cursor=first.next_cursor)
    assert empty.items == [] and empty.next_cursor
    assert not finish(ledger, gateway).coverage.invoice_search_exhausted
    invoke(ledger, gateway, "invoice.search", **args, cursor=empty.next_cursor)
    assert finish(ledger, gateway).status == "resolved"


@pytest.mark.parametrize("stale_cursor", [True, False])
def test_external_revision_suppresses_stale_membership_and_decision(sandbox, stale_cursor):
    gateway = ToolGateway(sandbox, PRINCIPAL)
    ledger = Ledger("review_invoices", customer_id=ACCOUNT)
    first = invoke(ledger, gateway, "invoice.search", customer_id=ACCOUNT, limit=2)
    assessment = invoke(ledger, gateway, "invoice.assess", invoice_id=TARGET)
    assert finish(ledger, gateway).decisions[0].outstanding_minor == assessment.outstanding_minor
    with closing(connect(sandbox.path)) as db:
        transition_payment(db, Principal(PRINCIPAL.tenant_id, "simulator"), "PMT-2", "posted", sandbox.clock)
    if stale_cursor:
        invoke(ledger, gateway, "invoice.search", customer_id=ACCOUNT, limit=2, cursor=first.next_cursor, ok=False)
    else:
        invoke(ledger, gateway, "invoice.get", invoice_id="unavailable", ok=False)
    output = finish(ledger, gateway)
    assert output.coverage.observed_invoice_ids == []
    assert output.decisions == output.evidence == []


def test_assigned_invoices_require_customer_membership_and_matching_evidence(sandbox):
    targets = frozenset({TARGET, "INV-M01"})
    gateway = ToolGateway(sandbox, PRINCIPAL)
    ledger = Ledger("review_invoices", customer_id=ACCOUNT, invoice_ids=targets)
    for identifier in sorted(targets):
        invoke(ledger, gateway, "invoice.get", invoice_id=identifier)
        invoke(ledger, gateway, "invoice.assess", invoice_id=identifier)
    output = finish(ledger, gateway)
    assert output.status == "partial"
    assert output.coverage.observed_invoice_ids == [TARGET]
    assert output.coverage.pending_invoice_ids == ["INV-M01"]
    assert [decision.invoice_id for decision in output.decisions] == [TARGET]

    def forged_lookup(identifier):
        observation = gateway.observation(identifier)
        if observation and observation.source_type == "invoice_assessment":
            return replace(observation, invoice_id="INV-105")
        return observation

    assert finish(ledger, gateway, lookup=forged_lookup).decisions == []


def test_receipt_requires_matching_readback_and_own_commit_preserves_search_coverage(sandbox):
    gateway = write_gateway(sandbox)
    ledger = Ledger("escalate", customer_id=ACCOUNT)
    page = invoke(ledger, gateway, "invoice.search", customer_id=ACCOUNT)
    assessments = {
        row.id: invoke(ledger, gateway, "invoice.assess", invoice_id=row.id)
        for row in page.items
    }
    invoke(ledger, gateway, "ticket.search", invoice_id="INV-106")
    receipt = invoke(ledger, gateway, "ticket.create", **command(preview(ledger, gateway, assessments[TARGET])))
    invoke(ledger, gateway, "ticket.get", ticket_id="TCK-901")
    partial = finish(ledger, gateway)
    assert partial.status == "partial"
    assert partial.actions[0].status == "committed_unverified"
    assert not partial.actions[0].confirmed_by_readback

    invoke(ledger, gateway, "ticket.get", ticket_id=receipt.result_id)
    output = finish(ledger, gateway)
    assert output.status == "resolved"
    assert output.coverage.state_revision == receipt.state_revision - 1
    assert output.coverage.pending_invoice_ids == []
    assert [(action.status, action.confirmed_by_readback) for action in output.actions] == [
        ("completed", True), ("already_active", False),
    ]
    evidence_ids = {observation.id for observation in output.evidence}
    assert all(set(item.evidence_ids) <= evidence_ids for item in [*output.decisions, *output.actions])


def test_lost_commit_replay_can_confirm_assigned_invoice(sandbox):
    targets = frozenset({TARGET})
    faults = FaultPlan((FaultStep("ticket.create", 1, FaultKind.LOSE_RESPONSE),))
    gateway = write_gateway(sandbox, invoice_ids=targets, faults=faults)
    ledger = Ledger("escalate", customer_id=ACCOUNT, invoice_ids=targets)
    assessment = invoke(ledger, gateway, "invoice.assess", invoice_id=TARGET)
    args = command(preview(ledger, gateway, assessment))
    invoke(ledger, gateway, "ticket.create", **args, ok=False)
    ledger.record_failure(TARGET, "ticket.create", uncertain=True, reason="TRANSIENT_FAILURE")
    assert finish(ledger, gateway).actions[0].status == "commit_unknown"
    replay = invoke(ledger, gateway, "ticket.create", **args)
    assert replay.replayed
    invoke(ledger, gateway, "ticket.get", ticket_id=replay.result_id)
    result = finish(ledger, gateway)
    assert result.status == "resolved"
    assert result.actions[0].status == "completed"


def test_unknown_commit_outweighs_suggested_completion(sandbox):
    targets = frozenset({TARGET})
    gateway = write_gateway(sandbox, invoice_ids=targets)
    ledger = Ledger("escalate", customer_id=ACCOUNT, invoice_ids=targets)
    assessment = invoke(ledger, gateway, "invoice.assess", invoice_id=TARGET)
    preview(ledger, gateway, assessment)
    ledger.record_failure(TARGET, "ticket.create", uncertain=True, reason="TRANSIENT_FAILURE")
    result = finish(ledger, gateway)
    assert result.status == "partial"
    assert result.review_required
    assert result.coverage.pending_invoice_ids == [TARGET]
    assert result.actions[0].status == "commit_unknown"
    assert result.actions[0].result_id is None


def test_approval_waits_for_verified_readback(sandbox):
    targets = frozenset({TARGET})
    principal = Principal("bluewater_services", "analyst")
    scope = WriteScope(principal, "approved", ACCOUNT, frozenset({Capability.REQUEST_APPROVAL}), targets)
    gateway = ToolGateway(sandbox, principal, write_scope=scope)
    ledger = Ledger("escalate", customer_id=ACCOUNT, invoice_ids=targets)
    assessment = invoke(ledger, gateway, "invoice.assess", invoice_id=TARGET)
    proposal = preview(ledger, gateway, assessment)
    assert proposal.decision == "approval_required"
    receipt = invoke(ledger, gateway, "approval.request", **command(proposal))

    assert finish(ledger, gateway).actions[0].status == "committed_unverified"
    invoke(ledger, gateway, "approval.get", approval_id=receipt.result_id)
    output = finish(ledger, gateway)
    assert output.status == "needs_review"
    assert output.review_required
    assert output.actions[0].status == "pending_approval"
    assert output.actions[0].confirmed_by_readback


def test_scoped_preview_can_confirm_an_already_active_ticket(sandbox):
    targets = frozenset({"INV-106"})
    gateway = write_gateway(sandbox, invoice_ids=targets)
    ledger = Ledger("escalate", customer_id=ACCOUNT, invoice_ids=targets)
    assessment = invoke(ledger, gateway, "invoice.assess", invoice_id="INV-106")
    assert preview(ledger, gateway, assessment).decision == "already_active"
    output = finish(ledger, gateway)
    assert output.status == "resolved"
    assert output.actions[0].status == "already_active"
    assert output.actions[0].result_id == "TCK-901"
    assert not output.actions[0].confirmed_by_readback
