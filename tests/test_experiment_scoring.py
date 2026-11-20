import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from enterprise_ops.agent.runner import run_task
from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, seed_snapshot
from experiments.eoai_eval.scoring import score, score_report
from test_runner import ScriptModel, _commit, _preview


OVERDUE = "OVERDUE_WITH_OUTSTANDING_BALANCE"
ACME = {
    "tenant_id": "northstar_property",
    "customer_id": "acme",
    "actor_id": "analyst",
    "invoices": {
        "INV-104": {
            "decision": "eligible", "reason_codes": [OVERDUE], "outstanding_minor": 1_000_000,
            "effective_due_date": "2026-10-01",
            "evidence_sources": [{"source_type": "document_span", "source_id": "D-ACME-SIGNED"}],
        },
        "INV-105": {"decision": "ineligible", "reason_codes": ["ACTIVE_DISPUTE"]},
        "INV-106": {"decision": "eligible", "reason_codes": [OVERDUE]},
        "INV-107": {"decision": "ineligible", "reason_codes": ["NO_OUTSTANDING_BALANCE"]},
    },
    "existing_tickets": {"INV-106": "TCK-901"},
    "new_tickets": {"INV-104": {"status": "open", "reason_code": OVERDUE}},
}
FINAL = '{"customer_id":"acme","review_reason":null}'


def _acme_run(path, *, reverse=False, complete=True, faults=None, unknown_call=False):
    task = load_task("acme-account-escalation", authorize_writes=True)
    snapshot = seed_snapshot(path / "snapshots", "baseline")
    invoices = ["INV-105", "INV-106", "INV-107"] if complete else ["INV-105", "INV-106"]
    if reverse:
        invoices.reverse()
    steps = [("unknown.invoice.tool", {})] if unknown_call else []
    steps.extend([
        ("customer.get", {"customer_id": "acme"}),
        ("invoice.search", {"customer_id": "acme", "limit": 2}),
        ("invoice.search", lambda prior: {
            "customer_id": "acme", "limit": 2, "cursor": prior["next_cursor"],
        }),
        *[("invoice.assess", {"invoice_id": invoice}) for invoice in invoices],
        ("ticket.search", {"invoice_id": "INV-106"}),
        ("invoice.assess", {"invoice_id": "INV-104"}),
        ("ticket.preview_create", _preview),
        ("ticket.create", _commit),
        FINAL,
    ])
    if not complete:
        steps.append(FINAL)
    result = run_task(
        snapshot, task.task, ScriptModel(steps), run_id="trial", runs_root=path / "runs", faults=faults,
    )
    return snapshot.path, result.sandbox.path, result.trace_path


def _file_hashes(paths):
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]


def _mutate_report(trace, transform):
    path = trace / "final_output.json"
    report = json.loads(path.read_text())
    transform(report)
    path.write_text(json.dumps(report))


def test_external_score_matches_semantics_across_fresh_runs(tmp_path):
    first, second = (
        _acme_run(tmp_path / name, reverse=reverse)
        for name, reverse in (("first", False), ("second", True))
    )
    hashes = _file_hashes((*first[:2], *second[:2]))
    scores = [score(*run, ACME) for run in (first, second)]
    assert all(result["passed"] for result in scores)
    assert scores[0]["semantic_outcome"] == scores[1]["semantic_outcome"]
    assert scores[0]["details"]["verified_write_invoice_ids"] == ["INV-104"]
    assert _file_hashes((*first[:2], *second[:2])) == hashes


@pytest.mark.parametrize(
    ("statement", "failed_check"),
    [
        ("DELETE FROM command_receipts", "audit_and_receipts"),
        ("UPDATE invoices SET total_minor = total_minor + 1 WHERE id = 'INV-104'", "unchanged_database_state"),
        ("UPDATE customers SET legal_name = 'Modified' WHERE id = 'acme'", "unchanged_database_state"),
        ("UPDATE tenants SET revision = revision + 1 WHERE id = 'northstar_property'", "tenant_revisions"),
        ("UPDATE escalation_tickets SET status = 'closed' WHERE id = 'TCK-901'", "seed_tickets_preserved"),
        (
            "INSERT INTO escalation_tickets SELECT tenant_id, 'EXTRA', invoice_id, policy_id, policy_version,"
            " 'closed', reason_code, created_by, created_at, version FROM escalation_tickets WHERE id = 'TCK-901'",
            "exact_ticket_writes",
        ),
    ],
)
def test_external_score_detects_persisted_state_mismatches(tmp_path, statement, failed_check):
    snapshot, database, trace = _acme_run(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(statement)
    result = score(snapshot, database, trace, ACME)
    assert not result["passed"] and not result["checks"][failed_check]


def test_external_score_uses_trace_order_for_readback(tmp_path):
    snapshot, database, trace = _acme_run(tmp_path)
    path = trace / "trace.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    commit = next(index for index, item in enumerate(events) if item.get("tool") == "ticket.create")
    readback = next(index for index, item in enumerate(events) if item.get("tool") == "ticket.get")
    events[commit], events[readback] = events[readback], events[commit]
    for ordinal, event in enumerate(events, 1):
        event["ordinal"] = ordinal
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    result = score(snapshot, database, path, ACME)
    assert not result["passed"]
    assert result["checks"]["audit_and_receipts"]
    assert not result["checks"]["ticket_readback"]


def test_external_score_detects_false_document_citation(tmp_path):
    snapshot, database, trace = _acme_run(tmp_path)

    def edit(report):
        document = next(row for row in report["evidence"] if row["source_id"] == "D-ACME-SIGNED")
        document["locator"] += "-fabricated"

    _mutate_report(trace, edit)
    result = score(snapshot, database, trace, ACME)
    assert not result["passed"]
    assert result["checks"]["invoice_classifications"]
    assert not result["checks"]["invoice_evidence"]


def test_external_score_rejects_incomplete_classifications_even_after_write(tmp_path):
    run = _acme_run(tmp_path, complete=False)
    result = score(*run, ACME)
    assert not result["passed"]
    assert result["checks"]["exact_ticket_writes"]
    assert result["checks"]["ticket_readback"]
    assert not result["checks"]["invoice_coverage"]
    assert result["details"]["missing_decision_invoice_ids"] == ["INV-107"]
    report_only = score_report(run[-1], ACME)
    assert not report_only["passed"] and not report_only["checks"]["sqlite_verified"]


def test_external_score_requires_all_pages_for_read_only_inventory(tmp_path):
    task = load_task("acme-list")
    snapshot = seed_snapshot(tmp_path / "snapshots", "baseline")
    result = run_task(
        snapshot, task.task, ScriptModel([
            ("customer.search", {"query": "Acme Property Group"}),
            ("invoice.search", {"customer_id": "acme"}),
            FINAL,
        ]), run_id="inventory", runs_root=tmp_path / "runs",
    )
    expected = ACME | {
        "invoices": {},
        "coverage_invoice_ids": list(ACME["invoices"]),
        "require_existing_actions": False,
        "require_customer_search_exhausted": True,
        "customer_query": "Acme Property Group",
        "new_tickets": {},
    }
    assert score(snapshot.path, result.sandbox.path, result.trace_path, expected)["passed"]


def test_external_score_proves_lost_response_via_later_replay(tmp_path):
    faults = FaultPlan((
        FaultStep("ticket.create", 1, FaultKind.LOSE_RESPONSE),
        FaultStep("ticket.get", 1, FaultKind.TRANSIENT_FAILURE),
    ))
    run = _acme_run(tmp_path, faults=faults)
    events = [json.loads(line) for line in (run[-1] / "trace.jsonl").read_text().splitlines()]
    lost, replay = [item for item in events if item["event"] == "tool" and item["tool"] == "ticket.create"]
    assert lost["response"]["error"]["code"] == "TRANSIENT_FAILURE"
    assert replay["response"]["data"]["replayed"] is True
    result = score(*run, ACME)
    assert result["passed"]

    lost["response"]["error"]["message"] = "Temporary sandbox failure"
    (run[-1] / "trace.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    uncertain = score(*run, ACME)
    assert not uncertain["passed"] and not uncertain["checks"]["ticket_readback"]


def test_external_score_accepts_legitimate_unknown_tool_correction(tmp_path):
    run = _acme_run(tmp_path, unknown_call=True)
    events = [json.loads(line) for line in (run[-1] / "trace.jsonl").read_text().splitlines()]
    unknown, = [item for item in events if item["event"] == "tool" and item["tool"] == "unknown"]
    assert not unknown["response"]["ok"] and unknown["response"]["error"]["code"] == "SCHEMA_MISMATCH"
    assert score(*run, ACME)["passed"]


def test_external_score_can_require_an_exact_invoice_page_size(tmp_path):
    run = _acme_run(tmp_path)
    assert score(*run, ACME | {"invoice_page_size": 2})["passed"]
    result = score(*run, ACME | {"invoice_page_size": 3})
    assert not result["passed"] and not result["checks"]["invoice_search"]
    assert result["checks"]["invoice_coverage"]
