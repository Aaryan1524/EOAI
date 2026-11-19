from contextlib import closing
from time import monotonic

import pytest

from enterprise_ops.domain.entities import Capability, Principal
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, seed_snapshot
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools import gateway as gateway_module
from enterprise_ops.tools.gateway import ToolGateway
from enterprise_ops.tools.session import WriteScope


PRINCIPAL = Principal("northstar_property", "analyst")
INVOICE_ID = "INV-104"


def prepare_command(gateway):
    assessment = gateway.invoke("invoice.assess", {"invoice_id": INVOICE_ID})
    assert assessment.ok
    preview = gateway.invoke("ticket.preview_create", {
        "invoice_id": INVOICE_ID,
        "reason_code": "OVERDUE_WITH_OUTSTANDING_BALANCE",
        "evidence_ids": assessment.data.evidence_ids,
    })
    assert preview.ok
    return {
        "preview_token": preview.data.preview_token,
        "idempotency_key": preview.data.idempotency_key,
        "expected_tenant_revision": preview.data.tenant_revision,
        "expected_invoice_version": preview.data.invoice_version,
        "expected_policy_version": preview.data.policy_version,
    }


@pytest.fixture
def command(tmp_path):
    sandbox = seed_snapshot(tmp_path / "snapshots", "baseline").clone(tmp_path / "runs", "deadline")
    scope = WriteScope(PRINCIPAL, "ticket-scope", "acme", frozenset({Capability.CREATE_TICKET}))
    gateway = ToolGateway(sandbox, PRINCIPAL, write_scope=scope)
    return sandbox, scope, gateway, prepare_command(gateway)


def assert_retryable(response):
    assert not response.ok
    assert response.error.code == "TRANSIENT_FAILURE"
    assert response.error.retryable
    assert response.meta.evidence_ids == []


def assert_unmodified(sandbox, arguments):
    with closing(connect(sandbox.path)) as db:
        revision = db.execute("SELECT revision FROM tenants WHERE id = ?", (PRINCIPAL.tenant_id,)).fetchone()[0]
        assert revision == arguments["expected_tenant_revision"]
        assert db.execute("SELECT count(*) FROM escalation_tickets WHERE invoice_id = ?", (INVOICE_ID,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0


@pytest.mark.parametrize("blocking_transaction", ["BEGIN IMMEDIATE", "BEGIN"])
def test_sqlite_lock_wait_is_bounded_and_leaves_no_command(command, blocking_transaction):
    sandbox, _, gateway, arguments = command
    with closing(connect(sandbox.path)) as blocker:
        blocker.execute(blocking_transaction)
        # A deferred transaction must read before it holds a shared lock that blocks COMMIT.
        blocker.execute("SELECT revision FROM tenants").fetchall()
        started = monotonic()
        response = gateway.invoke("ticket.create", arguments, timeout=0.05)
        assert monotonic() - started < 0.75
        assert_retryable(response)
        if blocking_transaction == "BEGIN":
            assert response.meta.state_revision == arguments["expected_tenant_revision"]
        blocker.rollback()

    assert_unmodified(sandbox, arguments)
    assert gateway.invoke("ticket.create", arguments).ok


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "1"])
def test_invalid_timeout_fails_closed(command, timeout):
    sandbox, _, gateway, arguments = command
    assert_retryable(gateway.invoke("ticket.create", arguments, timeout=timeout))
    assert_unmodified(sandbox, arguments)


def test_deadline_expires_after_dispatch_and_rolls_back_write(command, monkeypatch):
    sandbox, _, gateway, arguments = command
    now = [0.0]
    dispatch = gateway_module._dispatch

    def expire_after_dispatch(*args):
        result = dispatch(*args)
        now[0] += 1
        return result

    monkeypatch.setattr(gateway_module, "monotonic", lambda: now[0])
    monkeypatch.setattr(gateway_module, "_dispatch", expire_after_dispatch)
    response = gateway.invoke("ticket.create", arguments, timeout=0.5)
    assert_retryable(response)
    assert response.meta.state_revision == arguments["expected_tenant_revision"]
    assert_unmodified(sandbox, arguments)
    assert gateway.invoke("ticket.create", arguments).ok


def test_lost_response_with_timeout_preserves_committed_receipt(command):
    sandbox, scope, gateway, _ = command
    faults = FaultPlan((FaultStep("ticket.create", 1, FaultKind.LOSE_RESPONSE),))
    faulty = ToolGateway(sandbox, PRINCIPAL, write_scope=scope, faults=faults)
    arguments = prepare_command(faulty)
    lost = faulty.invoke("ticket.create", arguments, timeout=1)
    assert_retryable(lost)
    replay = gateway.invoke("ticket.create", arguments, timeout=1)
    assert replay.ok
    assert replay.data.replayed
    assert replay.data.creating_call_id == lost.meta.call_id
    assert replay.data.state_revision == lost.meta.state_revision


def test_deadline_expires_after_read_and_discards_observations(command, monkeypatch):
    _, _, gateway, _ = command
    now = [0.0]
    evidence_ids = []
    dispatch = gateway_module._dispatch

    def expire_after_read(call, *args):
        result = dispatch(call, *args)
        evidence_ids.extend(call.observations)
        now[0] += 1
        return result

    monkeypatch.setattr(gateway_module, "monotonic", lambda: now[0])
    monkeypatch.setattr(gateway_module, "_dispatch", expire_after_read)
    response = gateway.invoke("ticket.search", {"invoice_id": INVOICE_ID}, timeout=0.5)
    assert_retryable(response)
    assert evidence_ids
    assert all(gateway.observation(identifier) is None for identifier in evidence_ids)


def test_existing_receipt_remains_known_if_deadline_expires_after_read(command, monkeypatch):
    _, _, gateway, arguments = command
    committed = gateway.invoke("ticket.create", arguments)
    assert committed.ok
    now = [0.0]
    dispatch = gateway_module._dispatch

    def expire_after_replay(*args):
        result = dispatch(*args)
        now[0] += 1
        return result

    monkeypatch.setattr(gateway_module, "monotonic", lambda: now[0])
    monkeypatch.setattr(gateway_module, "_dispatch", expire_after_replay)
    replay = gateway.invoke("ticket.create", arguments, timeout=0.5)
    assert replay.ok
    assert replay.data.replayed
    assert replay.data.result_id == committed.data.result_id
    assert replay.data.creating_call_id == committed.meta.call_id
