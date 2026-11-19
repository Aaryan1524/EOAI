import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from threading import Barrier, Event
from time import monotonic

import pytest

from enterprise_ops.domain.entities import Capability, EscalationRestrictions, Principal
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, seed_snapshot
from enterprise_ops.storage.repository import Forbidden, open_dispute, transition_payment
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools.actions import decide_approval
from enterprise_ops.tools.gateway import TOOLS, ToolGateway
from enterprise_ops.tools.session import WriteScope


NORTH = Principal("northstar_property", "analyst")
BLUE = Principal("bluewater_services", "analyst")
REASON = "OVERDUE_WITH_OUTSTANDING_BALANCE"


@pytest.fixture
def sandbox(tmp_path):
    return seed_snapshot(tmp_path / "snapshots", "baseline").clone(tmp_path / "runs", "workflow")


def scope(customer="acme", invoices=None, principal=NORTH):
    return WriteScope(
        principal, "confirmed-scope", customer,
        frozenset({Capability.CREATE_TICKET, Capability.REQUEST_APPROVAL}),
        invoice_ids=invoices,
        restrictions=EscalationRestrictions(threshold_days=30, exclude_disputed=True),
    )


def get(gateway, tool, **arguments):
    response = gateway.invoke(tool, arguments)
    assert response.ok, response.model_dump()
    assert response.error is None
    assert response.meta.tool_name == tool
    return response.data


def assert_error(response, code):
    assert not response.ok
    assert response.data is None
    assert response.error.code == code
    return response.error


def preview(gateway, invoice_id="INV-104", approval_id=None, evidence_ids=None):
    if evidence_ids is None:
        evidence_ids = get(gateway, "invoice.assess", invoice_id=invoice_id).evidence_ids
    return get(
        gateway, "ticket.preview_create", invoice_id=invoice_id, reason_code=REASON,
        evidence_ids=evidence_ids, approval_id=approval_id,
    )


def command_args(proposal):
    return {
        "preview_token": proposal.preview_token,
        "idempotency_key": proposal.idempotency_key,
        "expected_tenant_revision": proposal.tenant_revision,
        "expected_invoice_version": proposal.invoice_version,
        "expected_policy_version": proposal.policy_version,
    }


def test_public_tools_scripted_acme_workflow(sandbox):
    gateway = ToolGateway(sandbox, NORTH, write_scope=scope())
    declarations = {item["name"]: item for item in gateway.declarations()}
    assert len(declarations) == 18
    calls = []

    def call(tool, **arguments):
        assert tool in declarations
        calls.append(tool)
        return get(gateway, tool, **arguments)

    now = call("clock.now")
    assert now.business_date == "2026-11-19"
    matches = call("customer.search", query="Acme Property Group")
    assert len(matches.items) == 2
    exact, = [hit for hit in matches.items if hit.match_kind == "exact"]
    account = call("customer.get", customer_id=exact.id)
    first = call("invoice.search", customer_id=account.id, limit=2)
    second = call("invoice.search", customer_id=account.id, limit=2, cursor=first.next_cursor)
    assert first.next_cursor and second.next_cursor is None
    policy = call("policy.get_escalation")
    assert policy.threshold_days == 30 and policy.exclude_disputed

    assessments = {}
    for row in first.items + second.items:
        invoice = call("invoice.get", invoice_id=row.id)
        assert invoice.contract_id == "C-202"
        assert call("payment.list_for_invoice", invoice_id=row.id).next_cursor is None
        assert call("dispute.list_for_invoice", invoice_id=row.id).next_cursor is None
        assessments[row.id] = call("invoice.assess", invoice_id=row.id)

    contract = call("contract.get", contract_id="C-202")
    amendments = call("contract.list_amendments", contract_id=contract.id)
    base = contract.clauses[0].span_ref
    excerpts = [call("document.get_excerpt", **base.model_dump())]
    for amendment in amendments.items:
        excerpts.append(call("document.get_excerpt", **amendment.changes[0].span_ref.model_dump()))
    amended = assessments["INV-104"]
    assert amended.outstanding_minor == 1_000_000 and amended.days_past_due == 49
    assert amended.terms.stated_due_date_differs
    assert set(amended.evidence_ids) & {excerpts[1].evidence_id, excerpts[2].evidence_id} == {
        excerpts[1].evidence_id,
    }
    assert assessments["INV-105"].active_dispute_ids == ["DSP-1"]
    assert assessments["INV-105"].status == "ineligible"
    assert assessments["INV-107"].outstanding_minor == 0

    eligible = [key for key, value in assessments.items() if value.status == "eligible"]
    assert eligible == ["INV-104", "INV-106"]
    missing = []
    for invoice_id in eligible:
        tickets = call("ticket.search", invoice_id=invoice_id, statuses=["open", "in_progress"])
        if not tickets.items:
            assert tickets.next_cursor is None
            missing.append(invoice_id)
        else:
            assert [ticket.id for ticket in tickets.items] == ["TCK-901"]
    assert missing == ["INV-104"]
    proposal = call(
        "ticket.preview_create", invoice_id="INV-104", reason_code=REASON,
        evidence_ids=amended.evidence_ids,
    )
    assert proposal.decision == "ready_to_create"
    receipt = call("ticket.create", **command_args(proposal))
    readback = call("ticket.get", ticket_id=receipt.result_id)
    assert (readback.invoice_id, readback.creation_audit_id, readback.creating_call_id) == (
        receipt.invoice_id, receipt.audit_id, receipt.creating_call_id,
    )
    assert len(calls) == 32
    with closing(connect(sandbox.path)) as db:
        event = db.execute("SELECT * FROM audit_events WHERE id = ?", (receipt.audit_id,)).fetchone()
        assert event["actor_id"] == NORTH.user_id and event["run_id"] == sandbox.run_id
        assert event["action"] == "ticket.create"
        assert json.loads(event["after_json"])["id"] == readback.id
        assert json.loads(event["evidence_json"]) == amended.evidence_ids
        assert db.execute("SELECT count(*) FROM escalation_tickets").fetchone()[0] == 2


def test_closed_schemas_and_scoped_not_found(sandbox):
    gateway = ToolGateway(sandbox, NORTH)
    assert_error(gateway.invoke("invoice.get", {"invoice_id": 104}), "VALIDATION_ERROR")
    assert_error(gateway.invoke("invoice.get", {"invoice_id": "INV-104", "tenant_id": BLUE.tenant_id}), "VALIDATION_ERROR")
    assert_error(gateway.invoke("invoice.search", {"customer_id": "acme", "limit": True}), "VALIDATION_ERROR")
    assert_error(gateway.invoke("invoice.get", {"invoice_id": "INV-104"}, schema_version="v2"), "SCHEMA_MISMATCH")
    assert_error(gateway.invoke("approval.decide", {}), "SCHEMA_MISMATCH")
    foreign = assert_error(gateway.invoke("invoice.get", {"invoice_id": "INV-B01"}), "NOT_FOUND")
    absent = assert_error(gateway.invoke("invoice.get", {"invoice_id": "missing"}), "NOT_FOUND")
    assert foreign == absent
    denied = ToolGateway(sandbox, Principal(NORTH.tenant_id, "missing"))
    assert_error(denied.invoke("invoice.get", {"invoice_id": "INV-B01"}), "PERMISSION_DENIED")


def test_signed_cursor_scope_stale_revision_and_reset(sandbox):
    gateway = ToolGateway(sandbox, NORTH)
    first = get(gateway, "invoice.search", customer_id="acme", limit=1)
    args = {"customer_id": "acme", "limit": 1, "cursor": first.next_cursor}
    assert_error(gateway.invoke("invoice.search", {**args, "limit": 2}), "VALIDATION_ERROR")
    assert_error(gateway.invoke("invoice.search", {**args, "cursor": "changed"}), "VALIDATION_ERROR")
    assert_error(ToolGateway(sandbox, BLUE).invoke("invoice.search", args), "VALIDATION_ERROR")
    with closing(connect(sandbox.path)) as db:
        transition_payment(db, Principal(NORTH.tenant_id, "simulator"), "PMT-2", "posted", sandbox.clock)
    assert_error(gateway.invoke("invoice.search", args), "STALE_REVISION")
    replacement = sandbox.reset()
    assert_error(gateway.invoke("clock.now", {}), "PRECONDITION_FAILED")
    new_gateway = ToolGateway(replacement, NORTH)
    assert_error(new_gateway.invoke("invoice.search", args), "VALIDATION_ERROR")
    restored = get(new_gateway, "invoice.assess", invoice_id="INV-104")
    assert restored.outstanding_minor == 1_000_000


def test_empty_continuation_page_preserves_last_delivered_invoice(sandbox):
    schedule = FaultPlan((
        FaultStep("invoice.search", 1, FaultKind.RATE_LIMITED),
        FaultStep("invoice.search", 3, FaultKind.EMPTY_PAGE),
        FaultStep("invoice.search", 4, FaultKind.EMPTY_PAGE),
    ))
    gateway = ToolGateway(sandbox, NORTH, faults=schedule)
    args = {"customer_id": "acme", "limit": 2}
    error = assert_error(gateway.invoke("invoice.search", args), "RATE_LIMITED")
    assert error.retryable and error.retry_after_ms > 0
    first = get(gateway, "invoice.search", **args)
    empty1 = get(gateway, "invoice.search", **args, cursor=first.next_cursor)
    empty2 = get(gateway, "invoice.search", **args, cursor=empty1.next_cursor)
    assert empty1.items == empty2.items == []
    assert len({first.next_cursor, empty1.next_cursor, empty2.next_cursor, None}) == 4
    last = get(gateway, "invoice.search", **args, cursor=empty2.next_cursor)
    assert [row.id for row in first.items + last.items] == ["INV-104", "INV-105", "INV-106", "INV-107"]
    assert last.next_cursor is None
    replay = ToolGateway(sandbox.reset(), NORTH, faults=schedule)
    assert_error(replay.invoke("invoice.search", args), "RATE_LIMITED")


def test_write_scope_and_evidence_must_match_subject_revision_and_session(sandbox):
    gateway = ToolGateway(sandbox, NORTH, write_scope=scope())
    unrelated = get(gateway, "document.get_excerpt", document_id="D-DECOY", document_version=1, span_id="terms")
    current = get(gateway, "invoice.assess", invoice_id="INV-104")
    args = {"invoice_id": "INV-104", "reason_code": REASON, "evidence_ids": current.evidence_ids}
    assert_error(
        gateway.invoke("ticket.preview_create", {**args, "evidence_ids": [*current.evidence_ids, unrelated.evidence_id]}),
        "PRECONDITION_FAILED",
    )
    assert_error(gateway.invoke("ticket.preview_create", {**args, "evidence_ids": ["fabricated"]}), "PRECONDITION_FAILED")
    assert_error(ToolGateway(sandbox, NORTH).invoke("ticket.preview_create", args), "PERMISSION_DENIED")
    same_user_fresh = ToolGateway(sandbox, NORTH, write_scope=scope())
    assert_error(same_user_fresh.invoke("ticket.preview_create", args), "PRECONDITION_FAILED")
    with pytest.raises(ValueError, match="different principal"):
        ToolGateway(sandbox, BLUE, write_scope=scope())
    viewer_principal = Principal(NORTH.tenant_id, "viewer")
    viewer = ToolGateway(sandbox, viewer_principal, write_scope=scope(principal=viewer_principal))
    assert_error(viewer.invoke("ticket.preview_create", args), "PERMISSION_DENIED")
    narrow = ToolGateway(sandbox, NORTH, write_scope=scope(invoices=frozenset({"INV-105"})))
    assert_error(narrow.invoke("ticket.preview_create", args), "PERMISSION_DENIED")
    assert_error(gateway.invoke("ticket.preview_create", {**args, "invoice_id": "INV-M01"}), "PERMISSION_DENIED")
    different = get(gateway, "invoice.assess", invoice_id="INV-105")
    assert_error(
        gateway.invoke("ticket.preview_create", {**args, "evidence_ids": different.evidence_ids}),
        "PRECONDITION_FAILED",
    )


def test_assessment_evidence_describes_the_exact_values(sandbox):
    gateway = ToolGateway(sandbox, NORTH, write_scope=scope())
    observed = get(gateway, "invoice.assess", invoice_id="INV-104")
    with closing(connect(sandbox.path)) as db:
        db.execute(
            "UPDATE invoices SET total_minor = total_minor + 1 WHERE tenant_id = ? AND id = 'INV-104'",
            (NORTH.tenant_id,),
        )
    response = gateway.invoke("ticket.preview_create", {
        "invoice_id": "INV-104", "reason_code": REASON, "evidence_ids": observed.evidence_ids,
    })
    assert "does not match" in assert_error(response, "PRECONDITION_FAILED").message


def test_stale_preview_rechecks_new_dispute(sandbox):
    gateway = ToolGateway(sandbox, NORTH, write_scope=scope())
    proposal = preview(gateway)
    with closing(connect(sandbox.path)) as db:
        open_dispute(
            db, Principal(NORTH.tenant_id, "simulator"), "INV-104", "RACE-DISPUTE",
            "Quantity mismatch", sandbox.clock, run_id=sandbox.run_id,
        )
        activity = db.execute("SELECT action, subject_id, state_revision FROM audit_events").fetchone()
        assert tuple(activity) == ("dispute.open", "RACE-DISPUTE", 2)
    assert_error(gateway.invoke("ticket.create", command_args(proposal)), "STALE_REVISION")
    refreshed = preview(gateway)
    assert refreshed.decision == "blocked" and refreshed.preview_token is None
    assert "ACTIVE_DISPUTE" in refreshed.reason_codes
    assert not get(gateway, "ticket.search", invoice_id="INV-104").items


def test_noneligible_review_and_active_ticket_previews_do_not_offer_writes(sandbox):
    gateway = ToolGateway(sandbox, NORTH, write_scope=scope())
    assert preview(gateway, "INV-105").decision == "blocked"
    existing = preview(gateway, "INV-106")
    assert existing.decision == "already_active" and existing.existing_ticket_id == "TCK-901"
    meridian = ToolGateway(sandbox, NORTH, write_scope=scope("meridian"))
    for invoice_id in ("INV-M01", "INV-M02"):
        proposal = preview(meridian, invoice_id)
        assert proposal.decision == "needs_review"
        assert proposal.preview_token is None and proposal.idempotency_key is None


def test_expired_preview_blocks_fresh_commit_but_not_exact_durable_replay(sandbox):
    seconds = [1.0]
    gateway = ToolGateway(sandbox, NORTH, write_scope=scope(), elapsed=lambda: seconds[0])
    expired = command_args(preview(gateway))
    seconds[0] += 61
    assert_error(gateway.invoke("ticket.create", expired), "PRECONDITION_FAILED")

    arguments = command_args(preview(gateway))
    receipt = get(gateway, "ticket.create", **arguments)
    assert not receipt.replayed
    seconds[0] += 61
    reconnect = ToolGateway(sandbox, NORTH, write_scope=scope(), elapsed=lambda: seconds[0])
    replay = get(reconnect, "ticket.create", **arguments)
    assert replay.model_dump(exclude={"replayed"}) == receipt.model_dump(exclude={"replayed"})
    assert replay.replayed
    assert_error(reconnect.invoke("ticket.create", {**arguments, "expected_invoice_version": 999}), "CONFLICT")
    narrowed = ToolGateway(sandbox, NORTH, write_scope=scope(invoices=frozenset({"INV-105"})))
    assert_error(narrowed.invoke("ticket.create", arguments), "PERMISSION_DENIED")
    with closing(connect(sandbox.path)) as db:
        db.execute("UPDATE users SET active = 0 WHERE tenant_id = ? AND id = ?", (NORTH.tenant_id, NORTH.user_id))
    assert_error(reconnect.invoke("ticket.create", arguments), "PERMISSION_DENIED")


def test_lost_commit_response_can_be_retried_and_verified(sandbox):
    gateway = ToolGateway(
        sandbox, NORTH, write_scope=scope(), faults=FaultPlan((FaultStep("ticket.create", 1, FaultKind.LOSE_RESPONSE),)),
    )
    arguments = command_args(preview(gateway))
    failed = gateway.invoke("ticket.create", arguments)
    assert assert_error(failed, "TRANSIENT_FAILURE").retryable
    assert failed.meta.evidence_ids == []
    receipt = get(gateway, "ticket.create", **arguments)
    readback = get(gateway, "ticket.get", ticket_id=receipt.result_id)
    assert receipt.replayed and receipt.creating_call_id == failed.meta.call_id
    assert readback.creation_audit_id == receipt.audit_id
    assert readback.creating_call_id == receipt.creating_call_id
    with closing(connect(sandbox.path)) as db:
        assert db.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 1


@pytest.mark.parametrize("same_command", [False, True])
def test_competing_sqlite_connections_do_not_duplicate_tickets(sandbox, same_command):
    first = ToolGateway(sandbox, NORTH, write_scope=scope())
    second = first if same_command else ToolGateway(sandbox, NORTH, write_scope=scope())
    first_args = command_args(preview(first))
    second_args = first_args if same_command else command_args(preview(second))
    start = Barrier(2)

    def submit(gateway, args):
        start.wait(timeout=5)
        return gateway.invoke("ticket.create", args)

    with ThreadPoolExecutor(max_workers=2) as clients:
        requests = [clients.submit(submit, first, first_args), clients.submit(submit, second, second_args)]
        responses = [request.result(timeout=10) for request in requests]
    if same_command:
        assert all(response.ok for response in responses)
        assert {response.data.replayed for response in responses} == {True, False}
        assert len({response.data.result_id for response in responses}) == 1
    else:
        assert sum(response.ok for response in responses) == 1
        assert_error(next(response for response in responses if not response.ok), "STALE_REVISION")
    with closing(connect(sandbox.path)) as db:
        assert db.execute("SELECT count(*) FROM escalation_tickets WHERE invoice_id = 'INV-104'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE action = 'ticket.create'").fetchone()[0] == 1


def test_audit_failure_rolls_back_ticket_receipt_and_revision(sandbox):
    gateway = ToolGateway(sandbox, NORTH, write_scope=scope())
    arguments = command_args(preview(gateway))
    with closing(connect(sandbox.path)) as db:
        db.execute(
            "CREATE TRIGGER simulate_audit_failure BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'test failure'); END;"
        )
        assert_error(gateway.invoke("ticket.create", arguments), "CONFLICT")
        assert db.execute("SELECT revision FROM tenants WHERE id = ?", (NORTH.tenant_id,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM escalation_tickets WHERE invoice_id = 'INV-104'").fetchone()[0] == 0
        db.execute("DROP TRIGGER simulate_audit_failure")
        assert get(gateway, "ticket.create", **arguments)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM audit_events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("INSERT OR REPLACE INTO audit_events SELECT * FROM audit_events")


def test_pending_approval_deduplicates_and_supervisor_grant_is_consumed(sandbox):
    gateway = ToolGateway(sandbox, BLUE, write_scope=scope(principal=BLUE))
    proposal = preview(gateway)
    assert proposal.decision == "approval_required"
    assert_error(gateway.invoke("ticket.create", command_args(proposal)), "PRECONDITION_FAILED")
    receipt = get(gateway, "approval.request", **command_args(proposal))
    record = get(gateway, "approval.get", approval_id=receipt.result_id)
    assert record.status == "pending" and record.creation_audit_id == receipt.audit_id
    other_proposal = preview(gateway)
    assert other_proposal.idempotency_key != proposal.idempotency_key
    duplicate = get(gateway, "approval.request", **command_args(other_proposal))
    assert duplicate.replayed and duplicate.audit_id == receipt.audit_id
    assert duplicate.result_id == receipt.result_id and duplicate.state_revision == receipt.state_revision
    with pytest.raises(Forbidden):
        decide_approval(sandbox, BLUE, record.id, approve=True)
    with closing(connect(sandbox.path)) as db:
        db.execute("UPDATE users SET role = 'supervisor' WHERE tenant_id = ? AND id = ?", (BLUE.tenant_id, BLUE.user_id))
        with pytest.raises(Forbidden, match="their own"):
            decide_approval(sandbox, BLUE, record.id, approve=True)
        db.execute("UPDATE users SET role = 'analyst' WHERE tenant_id = ? AND id = ?", (BLUE.tenant_id, BLUE.user_id))
    decide_approval(sandbox, Principal(BLUE.tenant_id, "supervisor"), record.id, approve=True)
    assert get(gateway, "approval.get", approval_id=record.id).status == "approved"
    commit_ready = preview(gateway, approval_id=record.id)
    assert commit_ready.decision == "ready_to_create"
    ticket = get(gateway, "ticket.create", **command_args(commit_ready))
    assert get(gateway, "ticket.get", ticket_id=ticket.result_id).creation_audit_id == ticket.audit_id
    approved = get(gateway, "approval.get", approval_id=record.id)
    assert approved.consumed_ticket_id == ticket.result_id and approved.creation_audit_id == receipt.audit_id
    with closing(connect(sandbox.path)) as db:
        assert db.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 3


def test_approval_does_not_survive_a_change_in_its_basis(sandbox):
    gateway = ToolGateway(sandbox, BLUE, write_scope=WriteScope(
        BLUE, "allow-disputed", "acme", frozenset({Capability.CREATE_TICKET, Capability.REQUEST_APPROVAL}),
    ))
    receipt = get(gateway, "approval.request", **command_args(preview(gateway)))
    decide_approval(sandbox, Principal(BLUE.tenant_id, "supervisor"), receipt.result_id, approve=True)
    with closing(connect(sandbox.path)) as db:
        open_dispute(
            db, Principal(BLUE.tenant_id, "simulator"), "INV-104", "POST-APPROVAL",
            "Contract question", sandbox.clock,
        )
    refreshed = get(gateway, "invoice.assess", invoice_id="INV-104")
    assert refreshed.status == "eligible"
    assert_error(gateway.invoke("ticket.preview_create", {
        "invoice_id": "INV-104", "reason_code": REASON, "approval_id": receipt.result_id,
        "evidence_ids": refreshed.evidence_ids,
    }), "PRECONDITION_FAILED")


def test_reset_drains_a_ticket_call_and_replacement_has_no_ticket(sandbox, monkeypatch):
    gateway = ToolGateway(sandbox, NORTH, write_scope=scope())
    command = command_args(preview(gateway))
    entered, resume = Event(), Event()
    definition = TOOLS["ticket.create"]

    def blocked_handler(call, args):
        entered.set()
        assert resume.wait(5)
        return definition.handler(call, args)

    monkeypatch.setitem(TOOLS, "ticket.create", replace(definition, handler=blocked_handler))
    with ThreadPoolExecutor(max_workers=2) as work:
        writing = work.submit(gateway.invoke, "ticket.create", command)
        assert entered.wait(5)
        resetting = work.submit(sandbox.reset)
        try:
            deadline = monotonic() + 5
            while sandbox.active and monotonic() < deadline:
                resume.wait(0.005)
            assert not sandbox.active and not resetting.done()
            assert_error(gateway.invoke("clock.now", {}), "PRECONDITION_FAILED")
        finally:
            resume.set()
        response = writing.result(timeout=5)
        replacement = resetting.result(timeout=5)

    assert response.ok and response.meta.sandbox_id == sandbox.id
    new_gateway = ToolGateway(replacement, NORTH, write_scope=scope())
    assert not get(new_gateway, "ticket.search", invoice_id="INV-104").items
    assert_error(new_gateway.invoke("ticket.create", command), "PRECONDITION_FAILED")
