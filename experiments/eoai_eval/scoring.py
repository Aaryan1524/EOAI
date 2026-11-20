"""Deterministic scoring over evaluator-held expectations and archived run evidence."""

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path


DECISION_FIELDS = (
    "decision", "reason_codes", "effective_due_date", "stated_due_date",
    "stated_due_date_differs", "days_past_due", "outstanding_minor", "currency",
)
TICKET_FIELDS = ("id", "invoice_id", "status", "reason_code", "policy_id", "policy_version",
                 "created_at", "version")
TICKET_ACTION = "create_escalation_ticket"
UNCHANGED_TABLES = (
    "users", "customers", "customer_aliases", "document_versions", "document_spans",
    "contracts", "payment_clauses", "amendments", "amendment_changes", "invoices",
    "payments", "disputes", "escalation_policies", "policy_exclusions",
)


def _load_run(trace, expected):
    path = trace / "trace.jsonl" if trace.is_dir() else trace
    report = json.loads(path.with_name("final_output.json").read_text(encoding="utf-8"))
    manifest = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
    inputs = json.loads(path.with_name("input.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    calls = [event for event in events if event["event"] == "tool"]
    ordinals_valid = [event["ordinal"] for event in events] == list(range(1, len(events) + 1))
    identifiers = [call["response"]["meta"]["call_id"] for call in calls]
    trace_identity = (
        ordinals_valid and len(identifiers) == len(set(identifiers))
        and inputs["tenant_id"] == expected["tenant_id"] and inputs["user_id"] == expected["actor_id"]
        and all(report[key] == manifest[key] for key in ("run_id", "task_id", "as_of"))
        and all(
            call["response"]["meta"]["profile"] == manifest["profile"]
            and call["response"]["meta"]["sandbox_id"] == manifest["sandbox_id"]
            and call["response"]["meta"]["tool_name"] == call["tool"]
            and call["as_of"] == manifest["as_of"]
            for call in calls
        )
    )
    return report, manifest, calls, trace_identity


def _data(call):
    return call["response"]["data"]


def _meta(call):
    return call["response"]["meta"]


def _calls_for(calls, tool):
    return [call for call in calls if call["tool"] == tool and call["response"]["ok"] is True]


def _projection(record, fields):
    return {key: record[key] for key in fields}


def _decision(record):
    result = _projection(record, DECISION_FIELDS)
    result["reason_codes"] = sorted(result["reason_codes"])
    return result


def _assessment(record):
    terms = record["terms"]
    return _decision({
        **record, "decision": record["status"],
        "stated_due_date": terms["stated_due_date"],
        "stated_due_date_differs": terms["stated_due_date_differs"],
    })


def _expected_decision_matches(decision, expected):
    return all(
        decision[key] == (sorted(expected[key]) if key == "reason_codes" else expected[key])
        for key in DECISION_FIELDS if key in expected
    )


def _normalized_query(value):
    return " ".join(value.casefold().split())


def _completed_searches(calls, tool):
    """Reconstruct completed page chains by public arguments and state revision."""
    pending = {}
    completed = []
    for call in _calls_for(calls, tool):
        args = call["arguments"]
        revision = _meta(call)["state_revision"]
        if tool == "invoice.search":
            query = args.get("customer_id", args.get("account_ref"))
            statuses = tuple(sorted(args.get("statuses") or []))
            size = args.get("limit", args.get("page_size", 10))
            cursor = args.get("cursor", args.get("page_cursor"))
        else:
            query = _normalized_query(args["query"])
            statuses = ()
            size = args.get("limit", 10)
            cursor = args.get("cursor")
        key = (query, statuses, size, revision)
        data = _data(call)
        ids = [
            item["id"] for item in data["items"]
            if tool == "invoice.search" or (
                item["match_kind"] == "exact" and _normalized_query(item["legal_name"]) == query
            )
        ]
        if cursor is not None:
            previous = pending.pop(key, None)
            if previous is None:
                continue
            ids = previous + ids
        if data["next_cursor"] is not None:
            pending[key] = ids
        elif len(ids) == len(set(ids)):
            completed.append((query, statuses, size, set(ids)))
    return completed


def _cited_evidence(item, call_ids, tenant):
    origin = call_ids.get(item["observed_call_id"])
    if origin is None:
        return False
    meta = _meta(origin)
    immutable = item["source_type"] in {"document_span", "audit_event"}
    source_parts = [
        meta["sandbox_id"], tenant, item["source_type"], item["source_id"], item["source_version"],
        item["locator"], None if immutable else item["state_revision"],
    ]
    encoded = json.dumps(source_parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    expected_id = "ev_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return (
        item["id"] == expected_id and item["id"] in meta["evidence_ids"]
        and item["state_revision"] == meta["state_revision"]
    )


def _invoice_results(report, calls, expected):
    customer_id = expected["customer_id"]
    invoice_expectations = expected["invoices"]
    coverage = report["coverage"]
    decisions = report["decisions"]
    expected_ids = set(invoice_expectations)
    expected_coverage = set(expected.get("coverage_invoice_ids", invoice_expectations))
    reported_ids = [row["invoice_id"] for row in decisions]
    assessment_calls = defaultdict(list)
    for call in _calls_for(calls, "invoice.assess"):
        assessment_calls[_data(call)["invoice_id"]].append(call)
    call_ids = {_meta(call)["call_id"]: call for call in calls}
    evidence = {row["id"]: row for row in report["evidence"]}
    statuses = tuple(sorted(expected.get("invoice_statuses", [])))
    required_search = expected.get("invoice_search_required", True)
    page_size = expected.get("invoice_page_size")
    invoice_search = any(
        customer == customer_id and filters == statuses and ids == expected_coverage
        and (page_size is None or limit == page_size)
        for customer, filters, limit, ids in _completed_searches(calls, "invoice.search")
    )
    customer_query = expected.get("customer_query")
    customer_search = any(
        ids == {customer_id} and (customer_query is None or _normalized_query(customer_query) == query)
        for query, _, _, ids in _completed_searches(calls, "customer.search")
    )

    matched = []
    cited = []
    for row in decisions:
        invoice_id = row["invoice_id"]
        wanted = invoice_expectations.get(invoice_id)
        if wanted is None:
            continue
        decision = _decision(row)
        matching_calls = [
            call for call in assessment_calls[invoice_id]
            if _assessment(_data(call)) == decision
        ]
        if not matching_calls or not _expected_decision_matches(decision, wanted):
            continue
        matched.append(invoice_id)
        linked = [evidence.get(identifier) for identifier in row["evidence_ids"]]
        if not linked or any(item is None for item in linked):
            continue
        if not all(_cited_evidence(item, call_ids, expected["tenant_id"]) for item in linked):
            continue
        if not any(
            item["source_type"] == "invoice_assessment"
            and item["source_id"] == invoice_id
            and item["observed_call_id"] in {_meta(call)["call_id"] for call in matching_calls}
            for item in linked
        ):
            continue
        if all(
            any(all(item.get(k) == v for k, v in source.items()) for item in linked)
            for source in wanted.get("evidence_sources", [])
        ):
            cited.append(invoice_id)

    observed_ids = coverage["observed_invoice_ids"]
    checks = {
        "customer": coverage["customer_id"] == customer_id,
        "customer_search": customer_search or not expected.get("require_customer_search_exhausted", False),
        "invoice_search": not required_search or (
            invoice_search and coverage["invoice_search_exhausted"] is True
        ),
        "observed_invoices": Counter(observed_ids) == Counter(expected_coverage),
        "invoice_coverage": Counter(reported_ids) == Counter(invoice_expectations.keys()),
        "invoice_classifications": set(matched) == expected_ids and len(matched) == len(expected_ids),
        "invoice_evidence": set(cited) == expected_ids and len(cited) == len(expected_ids),
    }
    detail = {
        "searched_invoice_ids": sorted({
            item["id"]
            for call in _calls_for(calls, "invoice.search")
            for item in _data(call)["items"]
            if item["customer_id"] == customer_id
        }),
        "assessed_invoice_ids": sorted(assessment_calls),
        "missing_decision_invoice_ids": sorted(expected_ids - set(reported_ids)),
        "unexpected_decision_invoice_ids": sorted(set(reported_ids) - expected_ids),
        "matching_decision_invoice_ids": sorted(set(matched)),
        "cited_decision_invoice_ids": sorted(set(cited)),
    }
    return checks, detail


def _report_result(report, calls, expected):
    checks, detail = _invoice_results(report, calls, expected)
    missing = Counter((item["code"], item["subject_id"]) for item in report["missing_information"])
    wanted_missing = Counter(
        (item["code"], item["subject_id"]) for item in expected.get("missing_information", [])
    )
    checks["missing_information"] = missing == wanted_missing
    wanted_status = expected.get("status", "resolved")
    needs_review = [
        key for key, value in expected["invoices"].items() if value["decision"] == "needs_review"
    ]
    wanted_review = expected.get("review_required", wanted_status == "needs_review" or bool(needs_review))
    checks["report_status"] = (
        report["status"] == wanted_status and report["review_required"] is wanted_review
    )
    checks["pending_invoices"] = Counter(report["coverage"]["pending_invoice_ids"]) == Counter(
        expected.get("pending_invoice_ids", needs_review)
    )
    semantic = {
        "status": report["status"],
        "review_required": report["review_required"],
        "customer_id": report["coverage"]["customer_id"],
        "invoice_search_exhausted": report["coverage"]["invoice_search_exhausted"],
        "observed_invoice_ids": sorted(report["coverage"]["observed_invoice_ids"]),
        "pending_invoice_ids": sorted(report["coverage"]["pending_invoice_ids"]),
        "decisions": sorted(
            (_projection(row, ("invoice_id",)) | _decision(row) for row in report["decisions"]),
            key=lambda row: json.dumps(row, sort_keys=True),
        ),
        "actions": sorted(
            (
                _projection(row, ("invoice_id", "type", "status", "confirmed_by_readback"))
                | {"reason_codes": sorted(row["reason_codes"])}
                for row in report["actions"]
            ),
            key=lambda row: json.dumps(row, sort_keys=True),
        ),
        "missing_information": sorted(
            ({"code": code, "subject_id": subject} for code, subject in missing.elements()),
            key=lambda row: json.dumps(row, sort_keys=True),
        ),
    }
    return {"checks": checks, "details": detail, "semantic_outcome": semantic}


def score_report(trace: Path, expected: dict) -> dict:
    """Report-only diagnostic for archives without both SQLite files."""
    report, _, calls, trace_identity = _load_run(trace, expected)
    result = _report_result(report, calls, expected)
    result["checks"]["trace_identity"] = trace_identity
    result["checks"]["sqlite_verified"] = False
    result["passed"] = False
    return result


def _rows(database):
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        return {
            "schema": Counter(tuple(row) for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema"
            )),
            "unchanged": {
                # These identifiers are fixed by the frozen schema.
                table: Counter(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
                for table in UNCHANGED_TABLES
            },
            "tenants": {row["id"]: dict(row) for row in connection.execute("SELECT * FROM tenants")},
            "invoice_scope": [dict(row) for row in connection.execute(
                "SELECT tenant_id, id, customer_id, status FROM invoices"
            )],
            "tickets": {(row["tenant_id"], row["id"]): dict(row) for row in connection.execute(
                "SELECT * FROM escalation_tickets"
            )},
            "audits": {(row["tenant_id"], row["id"]): dict(row) for row in connection.execute(
                "SELECT * FROM audit_events"
            )},
            "receipts": {
                tuple(row[key] for key in (
                    "tenant_id", "sandbox_id", "run_id", "actor_id", "action", "command_key"
                )): dict(row)
                for row in connection.execute("SELECT * FROM command_receipts")
            },
            "approvals": {(row["tenant_id"], row["id"]): dict(row) for row in connection.execute(
                "SELECT * FROM approval_requests"
            )},
        }


def _additions(seed, run):
    return [run[key] for key in sorted(run.keys() - seed.keys())]


def _preserved(seed, run):
    return all(run.get(key) == row for key, row in seed.items())


def _matching_write(ticket, audit, receipt, expected, manifest):
    actor = expected["actor_id"]
    tenant = expected["tenant_id"]
    return (
        ticket["tenant_id"] == tenant
        and ticket["created_by"] == actor and ticket["created_at"] == manifest["as_of"]
        and audit["tenant_id"] == receipt["tenant_id"] == tenant
        and audit["action"] == receipt["action"] == "ticket.create"
        and audit["actor_id"] == receipt["actor_id"] == actor
        and audit["run_id"] == receipt["run_id"] == manifest["run_id"]
        and receipt["sandbox_id"] == manifest["sandbox_id"]
        and audit["subject_id"] == receipt["result_id"] == ticket["id"]
        and receipt["invoice_id"] == ticket["invoice_id"]
        and audit["id"] == receipt["audit_id"]
        and audit["reason_code"] == ticket["reason_code"]
        and audit["observed_at"] == ticket["created_at"]
        and audit["before_json"] is None
        and all(
            json.loads(audit["after_json"]).get(key) == ticket[key]
            for key in ("id", "invoice_id", "status", "policy_id", "policy_version")
        )
    )


def _matching_readback(ticket, audit, calls):
    receipt = {
        "invoice_id": ticket["invoice_id"], "result_id": ticket["id"],
        "audit_id": audit["id"], "creating_call_id": audit["call_id"],
        "state_revision": audit["state_revision"],
    }
    create_calls = _calls_for(calls, "ticket.create")
    acknowledgements = [
        call for call in create_calls if _projection(_data(call), receipt) == receipt
    ]
    creators = [
        call for call in calls
        if call["tool"] == "ticket.create" and _meta(call)["call_id"] == audit["call_id"]
        and _meta(call)["state_revision"] == audit["state_revision"]
    ]
    if len(creators) != 1:
        return False
    creator = creators[0]
    if creator["response"]["ok"]:
        acknowledgement = (
            creator if creator in acknowledgements and _data(creator)["replayed"] is False else None
        )
    else:
        error = creator["response"]["error"]
        if not (
            error["code"] == "TRANSIENT_FAILURE"
            and error["message"] == "Tool response was lost" and error["retryable"] is True
        ):
            return False
        acknowledgement = next((
            call for call in acknowledgements
            if call["ordinal"] > creator["ordinal"]
            and _data(call)["replayed"] is True and call["arguments"] == creator["arguments"]
        ), None)
    if acknowledgement is None:
        return False
    readback = _projection(ticket, TICKET_FIELDS) | {
        "creation_audit_id": audit["id"], "creating_call_id": audit["call_id"],
    }
    return any(
        call["ordinal"] > acknowledgement["ordinal"]
        and call["arguments"].get("ticket_id") == ticket["id"]
        and _meta(call)["state_revision"] >= audit["state_revision"]
        and _projection(_data(call), readback) == readback
        for call in _calls_for(calls, "ticket.get")
    )


def _observed_existing(ticket_id, invoice_id, calls):
    for call in calls:
        if call["response"]["ok"] is not True:
            continue
        data = _data(call)
        if call["tool"] in {"ticket.search", "ticket.get"}:
            items = data["items"] if call["tool"] == "ticket.search" else [data]
            if any(
                row["id"] == ticket_id and row["invoice_id"] == invoice_id
                and row["status"] in {"open", "in_progress"} for row in items
            ):
                return True
        if (
            call["tool"] == "ticket.preview_create" and data["invoice_id"] == invoice_id
            and data["decision"] == "already_active" and data["existing_ticket_id"] == ticket_id
        ):
            return True
    return False


def _write_results(seed, run, calls, report, expected, manifest):
    tenant = expected["tenant_id"]
    customer = expected["customer_id"]
    statuses = expected.get("invoice_statuses", [])
    new_tickets = _additions(seed["tickets"], run["tickets"])
    audits = _additions(seed["audits"], run["audits"])
    receipts = _additions(seed["receipts"], run["receipts"])

    def invoice_matches(row):
        return row["tenant_id"] == tenant and row["customer_id"] == customer and (
            not statuses or row["status"] in statuses
        )

    scope = {row["id"] for row in seed["invoice_scope"] if invoice_matches(row)}
    expected_scope = set(expected.get("coverage_invoice_ids", expected["invoices"]))
    expected_tickets = expected["new_tickets"]
    expected_existing = expected["existing_tickets"]
    scoped_existing = {
        row["invoice_id"]: row["id"] for row in seed["tickets"].values()
        if row["tenant_id"] == tenant and row["invoice_id"] in expected_scope
        and row["status"] in {"open", "in_progress"}
    }
    ticket_count = Counter((row["tenant_id"], row["invoice_id"]) for row in new_tickets)
    checked = {}
    linked = 0
    for ticket in new_tickets:
        matching_pairs = [
            (audit, receipt) for audit in audits for receipt in receipts
            if _matching_write(ticket, audit, receipt, expected, manifest)
        ]
        if len(matching_pairs) != 1:
            continue
        linked += 1
        audit, _ = matching_pairs[0]
        if _matching_readback(ticket, audit, calls):
            checked[ticket["invoice_id"]] = (ticket["id"], audit["id"])
    commit_results = {
        (row["invoice_id"], row["result_id"], row["audit_id"])
        for row in receipts if row["action"] == "ticket.create"
    }
    trace_commits_match = all(
        (data["invoice_id"], data["result_id"], data["audit_id"]) in commit_results
        for data in map(_data, _calls_for(calls, "ticket.create"))
    ) and not _calls_for(calls, "approval.request")
    actions = report["actions"]
    action_ids = [row["invoice_id"] for row in actions]
    existing_actions = expected_existing if expected.get("require_existing_actions", True) else {}
    actions_match = Counter(action_ids) == Counter((*expected_tickets, *existing_actions))
    for row in actions:
        invoice_id = row["invoice_id"]
        if row["status"] == "already_active":
            result_id = existing_actions.get(invoice_id)
            matches = (
                result_id is not None and row["result_id"] == result_id
                and row["audit_id"] is None and row["confirmed_by_readback"] is False
                and row["reason_codes"] == ["ACTIVE_TICKET_EXISTS"]
                and _observed_existing(result_id, invoice_id, calls)
            )
        else:
            matches = (
                invoice_id in expected_tickets and invoice_id in checked
                and row["status"] == "completed" and row["confirmed_by_readback"] is True
                and row["reason_codes"] == [expected_tickets[invoice_id]["reason_code"]]
                and (row["result_id"], row["audit_id"]) == checked[invoice_id]
            )
        actions_match = actions_match and matches and row["type"] == TICKET_ACTION
    expected_revisions = {
        key: row | {"revision": row["revision"] + (len(expected_tickets) if key == tenant else 0)}
        for key, row in seed["tenants"].items()
    }
    audit_revisions = sorted(row["state_revision"] for row in audits)
    seed_tenant = seed["tenants"].get(tenant)
    checks = {
        "unchanged_database_state": seed["schema"] == run["schema"] and seed["unchanged"] == run["unchanged"],
        "tenant_revisions": seed_tenant is not None and run["tenants"] == expected_revisions and (
            audit_revisions == list(range(seed_tenant["revision"] + 1, expected_revisions[tenant]["revision"] + 1))
        ),
        "seed_invoice_scope": set(expected["invoices"]) <= expected_scope <= scope and (
            not expected.get("invoice_search_required", True) or expected_scope == scope
        ),
        "seed_tickets_preserved": _preserved(seed["tickets"], run["tickets"]),
        "existing_tickets": expected_existing == scoped_existing,
        "exact_ticket_writes": ticket_count == Counter((tenant, key) for key in expected_tickets) and all(
            _projection(row, expected_tickets[row["invoice_id"]]) == expected_tickets[row["invoice_id"]]
            for row in new_tickets
        ),
        "audit_and_receipts": (
            len(new_tickets) == len(audits) == len(receipts) == linked
            and _preserved(seed["audits"], run["audits"])
            and _preserved(seed["receipts"], run["receipts"])
            and seed["approvals"] == run["approvals"]
            and trace_commits_match
        ),
        "ticket_readback": len(checked) == len(new_tickets) and set(checked) == set(expected_tickets),
        "reported_ticket_actions": actions_match,
    }
    detail = {
        "new_tickets": sorted(
            (_projection(row, ("tenant_id", "invoice_id", "status", "reason_code")) for row in new_tickets),
            key=lambda row: json.dumps(row, sort_keys=True),
        ),
        "verified_write_invoice_ids": sorted(checked),
        "audit_events_added": len(audits),
        "receipts_added": len(receipts),
    }
    return checks, detail


def score(snapshot: Path, database: Path, trace: Path, expected: dict) -> dict:
    """Evaluate a run against independently authored expectations and persisted state.

    Expected data requires tenant_id, actor_id, customer_id, invoices, new_tickets,
    and existing_tickets. Invoice entries require decision and reason_codes and
    can pin the other DECISION_FIELDS and evidence_sources. New ticket entries
    require status and reason_code. Existing ticket entries map invoice to ticket.
    """
    for decision in expected["invoices"].values():
        _projection(decision, ("decision", "reason_codes"))
    for ticket in expected["new_tickets"].values():
        _projection(ticket, ("status", "reason_code"))
    report, manifest, calls, trace_identity = _load_run(trace, expected)
    result = _report_result(report, calls, expected)
    checks, detail = _write_results(_rows(snapshot), _rows(database), calls, report, expected, manifest)
    result["checks"].update(checks)
    result["checks"]["trace_identity"] = trace_identity
    result["checks"]["snapshot_matches_manifest"] = (
        hashlib.sha256(snapshot.read_bytes()).hexdigest() == manifest["snapshot_digest"]
    )
    result["checks"]["sqlite_verified"] = True
    result["details"].update(detail)
    result["semantic_outcome"]["new_tickets"] = detail["new_tickets"]
    result["semantic_outcome"]["verified_write_invoice_ids"] = detail["verified_write_invoice_ids"]
    result["passed"] = all(result["checks"].values())
    return result
