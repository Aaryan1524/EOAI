"""Load the small, deterministic development scenario."""

import json
import sqlite3
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from enterprise_ops.clock import FrozenClock
from enterprise_ops.storage.sqlite import transaction


def seed_baseline(connection: sqlite3.Connection) -> FrozenClock:
    scenario = Path(__file__).with_name("scenarios") / "baseline.json"
    fixture = json.loads(scenario.read_text(encoding="utf-8"))
    clock = FrozenClock(datetime.fromisoformat(fixture["simulation_time"]))
    with transaction(connection, write=True):
        if connection.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
            raise ValueError("Seed into an empty database")

        for tenant in fixture["tenants"]:
            ZoneInfo(tenant["timezone"])
        connection.executemany(
            "INSERT INTO tenants (id, name, timezone, revision) VALUES (:id, :name, :timezone, 1)",
            fixture["tenants"],
        )
        connection.executemany(
            "INSERT INTO escalation_policies "
            "(tenant_id, id, version, threshold_days, exclude_disputed, requires_approval) "
            "VALUES (:tenant, :id, :version, :threshold, :exclude_disputed, :requires_approval)",
            fixture["policies"],
        )
        connection.executemany(
            "UPDATE tenants SET default_policy_id = :policy_id, default_policy_version = :policy_version "
            "WHERE id = :id",
            fixture["tenants"],
        )
        connection.executemany(
            "INSERT INTO policy_exclusions (tenant_id, policy_id, policy_version, subject, status) "
            "VALUES (:tenant, :policy_id, :version, :subject, :status)",
            fixture["exclusions"],
        )
        connection.executemany(
            "INSERT INTO users (tenant_id, id, role) VALUES (:tenant, :id, :role)",
            fixture["users"],
        )
        connection.executemany(
            "INSERT INTO customers (tenant_id, id, legal_name, status) "
            "VALUES (:tenant, :id, :name, :status)",
            fixture["customers"],
        )
        connection.executemany(
            "INSERT INTO customer_aliases (tenant_id, customer_id, normalized_alias) VALUES (?, ?, ?)",
            (
                (customer["tenant"], customer["id"], " ".join(alias.casefold().split()))
                for customer in fixture["customers"]
                for alias in customer["aliases"]
            ),
        )
        for document in fixture["documents"]:
            _insert_document(connection, document)
        connection.executemany(
            "INSERT INTO contracts (tenant_id, id, customer_id, status, signed_at, issue_from, issue_to, "
            "source_document_id, source_document_version) "
            "VALUES (:tenant, :id, :customer, :status, :signed_at, :issue_from, :issue_to, "
            ":document, CASE WHEN :document IS NOT NULL THEN 1 END)",
            fixture["contracts"],
        )
        connection.executemany(
            "INSERT INTO payment_clauses "
            "(tenant_id, contract_id, id, net_days, grace_days, next_weekday, "
            "document_id, document_version, span_id) "
            "VALUES (:tenant, :contract, 'payment_terms', :net_days, 0, 0, :document, "
            "CASE WHEN :document IS NOT NULL THEN 1 END, "
            "CASE WHEN :document IS NOT NULL THEN 'terms' END)",
            fixture["clauses"],
        )
        connection.executemany(
            "INSERT INTO amendments "
            "(tenant_id, contract_id, id, status, signed_at, activation_on, issue_from, issue_to, "
            "priority, source_document_id, source_document_version) "
            "VALUES (:tenant, :contract, :id, :status, :signed_at, :activation_on, :issue_from, :issue_to, "
            ":priority, :document, CASE WHEN :document IS NOT NULL THEN 1 END)",
            fixture["amendments"],
        )
        connection.executemany(
            "INSERT INTO amendment_changes "
            "(tenant_id, contract_id, amendment_id, clause_id, field, value, "
            "document_id, document_version, span_id) "
            "VALUES (:tenant, :contract, :id, 'payment_terms', 'net_days', :net_days, :document, "
            "CASE WHEN :document IS NOT NULL THEN 1 END, "
            "CASE WHEN :document IS NOT NULL THEN 'terms' END)",
            fixture["amendments"],
        )
        connection.executemany(
            "INSERT INTO invoices "
            "(tenant_id, id, customer_id, contract_id, issued_at, stated_due_date, currency, total_minor, status) "
            "VALUES (:tenant, :id, :customer, :contract, :issued_at, :due, 'USD', :total, 'issued')",
            fixture["invoices"],
        )
        connection.executemany(
            "INSERT INTO payments "
            "(tenant_id, id, invoice_id, kind, status, amount_minor, currency, posted_at) "
            "VALUES (:tenant, :id, :invoice, 'payment', :status, :amount, 'USD', :posted_at)",
            fixture["payments"],
        )
        connection.executemany(
            "INSERT INTO disputes (tenant_id, id, invoice_id, opened_at, reason) "
            "VALUES (:tenant, :id, :invoice, :opened_at, :reason)",
            fixture["disputes"],
        )
        connection.executemany(
            "INSERT INTO escalation_tickets "
            "(tenant_id, id, invoice_id, policy_id, policy_version, status, reason_code, created_by, created_at) "
            "VALUES (:tenant, :id, :invoice, :policy_id, :version, 'open', "
            "'OVERDUE_WITH_OUTSTANDING_BALANCE', :actor, :created_at)",
            fixture["tickets"],
        )
    return clock


def _insert_document(connection: sqlite3.Connection, document: dict[str, str]) -> None:
    tenant = document["tenant"]
    identifier = document["id"]
    text = document["text"]
    connection.execute(
        "INSERT INTO document_versions "
        "(tenant_id, id, version, title, kind, source_uri, content, content_hash) "
        "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
        (
            tenant, identifier, document["title"], document["kind"],
            f"fixture://baseline/{tenant}/{identifier}", text, sha256(text.encode("utf-8")).hexdigest(),
        ),
    )
    connection.execute(
        "INSERT INTO document_spans "
        "(tenant_id, document_id, document_version, id, start_offset, end_offset) "
        "VALUES (?, ?, 1, 'terms', 0, ?)",
        (tenant, identifier, len(text)),
    )
