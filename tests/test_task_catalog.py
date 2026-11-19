import json
from contextlib import closing
from datetime import date

import pytest

from enterprise_ops.domain.entities import Capability
from enterprise_ops.fixtures.catalog import load_task, task_names
from enterprise_ops.sandbox import FaultKind, seed_snapshot
from enterprise_ops.storage.repository import assess_invoice, authorize
from enterprise_ops.storage.sqlite import connect


def _snapshot(tmp_path, entry):
    return seed_snapshot(tmp_path, entry.task.id, profile=entry.profile, scenario=entry.scenario)


def test_catalog_assigns_only_explicitly_authorized_write_scope():
    assert task_names() == (
        "acme-list", "acme-escalation", "meridian-conflict", "acme-paged-v2", "compound-escalation",
    )
    for name in task_names():
        assert load_task(name).task.write_scope is None

    for name, customer_id, invoice_ids in (
        ("acme-escalation", "acme", frozenset({"INV-104"})),
        ("compound-escalation", "harborline", None),
    ):
        task = load_task(name, authorize_writes=True).task
        assert task.write_scope is not None
        assert task.write_scope.principal == task.principal
        assert task.write_scope.customer_id == task.customer_id == customer_id
        assert task.write_scope.invoice_ids == task.invoice_ids == invoice_ids
        assert task.write_scope.capabilities == {Capability.CREATE_TICKET}

    for name in ("acme-list", "meridian-conflict", "acme-paged-v2"):
        assert load_task(name, authorize_writes=True).task.write_scope is None
    with pytest.raises(ValueError, match="Unknown built-in task"):
        load_task("unknown")
    with pytest.raises(ValueError, match="must be a boolean"):
        load_task("acme-escalation", authorize_writes="yes")


def test_compound_faults_are_independent_for_each_run():
    first = load_task("compound-escalation").faults
    second = load_task("compound-escalation").faults
    assert first.next_for("invoice.get") is None
    assert first.next_for("invoice.search") == FaultKind.TRANSIENT_FAILURE
    assert first.next_for("invoice.search") is None
    assert second.next_for("invoice.search") == FaultKind.TRANSIENT_FAILURE


def test_paged_acme_extends_the_baseline_with_amendment_boundaries(tmp_path):
    entry = load_task("acme-paged-v2")
    snapshot = _snapshot(tmp_path, entry)
    baseline = seed_snapshot(tmp_path, "baseline")

    tenant = entry.task.principal.tenant_id
    with closing(connect(snapshot.path)) as connection:
        aliases = connection.execute(
            "SELECT customer_id FROM customer_aliases WHERE tenant_id = ? AND normalized_alias = ? "
            "ORDER BY customer_id", (tenant, entry.task.customer_query.casefold()),
        ).fetchall()
        assert [row[0] for row in aliases] == ["acme", "acme-west"]
        invoices = connection.execute(
            "SELECT id FROM invoices WHERE tenant_id = ? AND customer_id = ? ORDER BY id", (tenant, "acme"),
        ).fetchall()
        assert [row[0] for row in invoices] == [f"INV-{number}" for number in range(104, 111)]
        for invoice_id, expected_due in (
            ("INV-108", date(2026, 8, 30)), ("INV-109", date(2026, 11, 14)), ("INV-110", date(2026, 10, 31)),
        ):
            assessment = assess_invoice(connection, entry.task.principal, invoice_id, snapshot.clock)
            assert assessment.terms.effective_due_date == expected_due

    with closing(connect(baseline.path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM invoices WHERE tenant_id = ? AND customer_id = ?", (tenant, "acme"),
        ).fetchone()[0] == 4
    assert (entry.level, snapshot.profile) == (3, "enterprise_ops:v2")


def test_compound_fixture_has_one_new_ticket_candidate_and_distinct_review_reasons(tmp_path):
    entry = load_task("compound-escalation", authorize_writes=True)
    snapshot = _snapshot(tmp_path, entry)
    principal = entry.task.principal
    assert (entry.level, snapshot.profile) == (4, "enterprise_ops:v2")

    with closing(connect(snapshot.path)) as connection:
        authorize(connection, principal, Capability.CREATE_TICKET)
        tenants = connection.execute("SELECT id FROM tenants").fetchall()
        assert [row[0] for row in tenants] == [principal.tenant_id]
        invoices = connection.execute(
            "SELECT id FROM invoices WHERE tenant_id = ? AND customer_id = ? ORDER BY id",
            (principal.tenant_id, entry.task.customer_id),
        ).fetchall()
        assessments = {
            row[0]: assess_invoice(connection, principal, row[0], snapshot.clock) for row in invoices
        }
        active = connection.execute(
            "SELECT invoice_id, id FROM escalation_tickets WHERE tenant_id = ? AND status = 'open'",
            (principal.tenant_id,),
        ).fetchall()

    assert dict(active) == {"INV-H02": "TCK-H02"}
    eligible = {invoice_id for invoice_id, value in assessments.items() if value.status == "eligible"}
    assert eligible == {"INV-H01", "INV-H02"}
    assert assessments["INV-H01"].balance.outstanding_minor == 800_000
    assert assessments["INV-H01"].terms.effective_due_date == date(2026, 8, 24)
    assert assessments["INV-H03"].status == "needs_review"
    assert assessments["INV-H03"].reason_codes == ("AMBIGUOUS_AMENDMENT",)
    assert {json.loads(ref)[1] for ref in assessments["INV-H03"].terms.source_refs} == {
        "D-HARBORLINE", "D-HARBORLINE-SUMMER", "D-HARBORLINE-AUGUST",
    }
    assert assessments["INV-H04"].reason_codes == ("ACTIVE_DISPUTE",)
    assert assessments["INV-H05"].reason_codes == ("NO_OUTSTANDING_BALANCE",)
    assert assessments["INV-H06"].reason_codes == ("OVERDUE_THRESHOLD_NOT_MET",)


def test_unknown_scenario_cannot_publish_snapshot(tmp_path):
    with pytest.raises(ValueError, match="Unknown fixture scenario"):
        seed_snapshot(tmp_path, "bad", scenario="../baseline")
    assert list(tmp_path.iterdir()) == []
