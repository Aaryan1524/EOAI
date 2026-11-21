"""Generate fresh, disjoint EOAI cases for the comparative experiment.

Every case receives its own tenant, customer names, invoice identifiers and SQLite
lineage. Outcomes are targeted with an independent hand calculation and then
cross-checked against the frozen policy engine; any disagreement aborts
generation. Outputs are custodian-private manifests in the held-out schema, so
they load through ``experiments.eoai_eval.datasets.load_heldout`` and its
disjointness validator without changing frozen source.
"""

import argparse
import json
import os
import random
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import file_digest, sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import EscalationRestrictions
from enterprise_ops.storage.repository import load_assessment
from enterprise_ops.storage.sqlite import connect, initialize, transaction

GENERATOR_SEED = 20261120
FAMILIES = ("inventory", "review", "conflict", "escalate_one", "escalate_account", "paged_review")
# Stratified split sizes per family. Totals: adaptation 20, gate 10, deveval 30.
ALLOCATION = {
    "adaptation": {"inventory": 3, "review": 3, "conflict": 3, "escalate_one": 4,
                   "escalate_account": 4, "paged_review": 3},
    "gate": {"inventory": 1, "review": 2, "conflict": 2, "escalate_one": 2,
             "escalate_account": 2, "paged_review": 1},
    "deveval": {family: 5 for family in FAMILIES},
}
SPLIT_CODE = {"adaptation": "a", "gate": "g", "deveval": "e"}
SIM_TIMES = ("2026-11-19T20:00:00+00:00", "2026-11-24T19:00:00+00:00",
             "2026-12-02T18:30:00+00:00", "2026-12-09T21:00:00+00:00")
TIMEZONES = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles")
_FIRST = ("Cobalt", "Juniper", "Granite", "Lantern", "Orchard", "Summit", "Redwood", "Beacon", "Willow",
          "Copper", "Prairie", "Silverline", "Tidewater", "Kestrel", "Maple", "Evergreen", "Ironbridge",
          "Sable", "Highland", "Crescent", "Falcon", "Birchwood", "Larkspur", "Northgate", "Riverbend",
          "Stonefield", "Clearwater", "Ashford", "Brightwell", "Foxglove", "Hollow Creek", "Pinecrest",
          "Quarry", "Saltmarsh", "Thornbury", "Upland", "Vantage", "Westbrook", "Yarrow", "Zephyr")
_SECOND = ("Facilities", "Property", "Logistics", "Hospitality", "Health", "Housing", "Retail",
           "Storage", "Maintenance", "Realty", "Campus", "Clinics", "Hotels", "Offices", "Warehousing")
_SUFFIX = ("LLC", "Inc.", "Group", "Partners", "Holdings", "Co.")
_TENANT_KIND = ("Receivables", "Operations", "Billing", "Finance", "Collections", "Accounts")
_RESERVED = ("acme", "meridian", "harborline", "northstar", "bluewater")

TEMPLATES = {
    "inventory": (
        "Produce a complete list of the invoices on file for {name}.",
        "Show every invoice recorded for the customer {name}.",
        "I need an inventory of all invoices belonging to {name}.",
    ),
    "review": (
        "Assess invoice {inv} for {name} against the current escalation policy. Report its "
        "contractual due date, days overdue, outstanding balance and eligibility.",
        "For {name}, determine whether invoice {inv} is eligible for escalation today. Use the "
        "contractual payment terms rather than the recorded due date.",
        "Check the escalation status of {name} invoice {inv}: work out the controlling due date "
        "and balance and state whether it qualifies.",
    ),
    "conflict": (
        "Work out the controlling payment terms for {name} invoice {inv} and whether it can be "
        "escalated. Cite the contract sources and raise anything that needs human review.",
        "Determine the due date that governs {name} invoice {inv}, citing the agreements. If the "
        "terms cannot be resolved, flag the invoice for review.",
        "Examine the contract terms behind {name} invoice {inv} and report its escalation status, "
        "flagging any unresolved term question for review.",
    ),
    "escalate_one": (
        "Assess {name} invoice {inv} under the current policy and, if it qualifies, open and "
        "confirm an escalation ticket (you are authorized to do so).",
        "Decide whether invoice {inv} for {name} should be escalated now. When it qualifies, "
        "create the escalation ticket and confirm it was recorded.",
        "Evaluate {name} invoice {inv} for escalation. If it is eligible, file the ticket and "
        "read it back to verify it.",
    ),
    "escalate_account": (
        "Go through every invoice for {name}. Using contractual due dates, find unpaid invoices "
        "more than {days} days overdue. Leave out disputed invoices and any that already have an "
        "open ticket. Open and confirm a ticket for each one that qualifies.",
        "Audit all of {name}'s invoices for escalation. An invoice qualifies when it is unpaid, "
        "over {days} days past its contractual due date, not disputed and not already ticketed. "
        "Create and verify a ticket for every qualifying invoice.",
        "Review the full invoice list for {name} and escalate each unpaid invoice that is more "
        "than {days} days overdue by contract terms, skipping disputes and existing tickets. "
        "Confirm every ticket you create.",
    ),
    "paged_review": (
        "Search for the customer {name} and confirm the account by its exact legal name. Then "
        "review all of its invoices, requesting three per page and following every continuation "
        "cursor. Compare recorded and contractual due dates and cite the governing terms.",
        "Identify {name} through customer search using the exact legal name. Page through every "
        "invoice three at a time until no cursor remains, and for each one compare the stated and "
        "contractual due dates with citations.",
        "Locate the account for {name} by exact legal name via search, then assess each of its "
        "invoices fetched in pages of three, following all cursors. Report recorded versus "
        "contractual due dates and cite the controlling terms.",
    ),
}


@dataclass
class World:
    tenant: str
    tenant_name: str
    timezone: str
    sim: datetime
    threshold: int
    fixture: dict = field(default_factory=dict)

    @property
    def business_date(self) -> date:
        return self.sim.astimezone(ZoneInfo(self.timezone)).date()


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.casefold()).strip("-").replace("--", "-")


class Namer:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.used: set[str] = set()

    def company(self) -> str:
        for _ in range(1000):
            name = f"{self.rng.choice(_FIRST)} {self.rng.choice(_SECOND)} {self.rng.choice(_SUFFIX)}"
            key = name.split()[0].casefold()
            if name not in self.used and not any(word in name.casefold() for word in _RESERVED):
                self.used.add(name)
                return name
        raise RuntimeError("Name space exhausted")

    def code(self, length: int = 3) -> str:
        for _ in range(1000):
            value = "".join(self.rng.choice("BCDFGHJKLMNPQRSTVWXZ") for _ in range(length))
            if value not in self.used:
                self.used.add(value)
                return value
        raise RuntimeError("Code space exhausted")


# ---------------------------------------------------------------- hand calculator (independent)

def hand_terms(issued_on, contract, world):
    """Independent re-statement of the contractual due-date rules used to target outcomes."""
    if contract["document"] is None:
        return None, "MISSING_TERM_EVIDENCE", []
    refs = [contract["document"]]
    applicable = []
    for amendment in contract["amendments"]:
        if amendment["status"] != "signed":
            continue
        if datetime.fromisoformat(amendment["signed_at"]) > world.sim:
            continue
        activation = date.fromisoformat(amendment["activation_on"])
        if activation > world.business_date:
            continue
        start = date.fromisoformat(amendment["issue_from"]) if amendment["issue_from"] else activation
        end = date.fromisoformat(amendment["issue_to"]) if amendment["issue_to"] else None
        if start <= issued_on and (end is None or issued_on < end):
            applicable.append(amendment)
    net = contract["net_days"]
    if applicable:
        top = max(a["priority"] for a in applicable)
        winners = [a for a in applicable if a["priority"] == top]
        refs += [a["document"] for a in winners]
        values = {a["net_days"] for a in winners}
        if len(values) > 1:
            return None, "AMBIGUOUS_AMENDMENT", sorted(set(refs))
        net = values.pop()
    return issued_on + timedelta(days=net), None, sorted(set(refs))


def hand_decision(invoice, contract, world, *, exclude_disputed=True):
    issued = datetime.fromisoformat(invoice["issued_at"])
    issued_on = issued.astimezone(ZoneInfo(world.timezone)).date()
    due, reason, refs = hand_terms(issued_on, contract, world)
    posted = sum(p["amount"] for p in invoice["payments"]
                 if p["status"] == "posted" and datetime.fromisoformat(p["posted_at"]) <= world.sim)
    outstanding = invoice["total"] - posted
    review = [reason] if reason else []
    blockers = []
    if outstanding == 0:
        blockers.append("NO_OUTSTANDING_BALANCE")
    overdue = None if due is None else (world.business_date - due).days
    if overdue is not None and overdue <= world.threshold:
        blockers.append("OVERDUE_THRESHOLD_NOT_MET")
    if invoice["dispute"] and exclude_disputed:
        blockers.append("ACTIVE_DISPUTE")
    status = "needs_review" if review else "ineligible" if blockers else "eligible"
    stated = date.fromisoformat(invoice["due"])
    return {
        "decision": status,
        "reason_codes": sorted(review + blockers) or ["OVERDUE_WITH_OUTSTANDING_BALANCE"],
        "effective_due_date": due.isoformat() if due else None,
        "stated_due_date": stated.isoformat(),
        "stated_due_date_differs": None if due is None else stated != due,
        "days_past_due": overdue,
        "outstanding_minor": outstanding,
        "currency": "USD",
        "evidence_sources": [{"source_type": "document_span", "source_id": ref} for ref in refs],
    }


# ---------------------------------------------------------------- world construction

def new_world(rng: random.Random, namer: Namer, code: str) -> World:
    tenant_label = f"{rng.choice(_FIRST)} {rng.choice(_TENANT_KIND)}"
    while any(word in tenant_label.casefold() for word in _RESERVED):
        tenant_label = f"{rng.choice(_FIRST)} {rng.choice(_TENANT_KIND)}"
    tenant = f"{_slug(tenant_label).replace('-', '_')}_{code.lower()}"
    world = World(tenant, tenant_label, rng.choice(TIMEZONES),
                  datetime.fromisoformat(rng.choice(SIM_TIMES)), rng.choice((20, 25, 30, 35)))
    old = rng.choice((15, 45))
    world.fixture = {
        "simulation_time": world.sim.isoformat(),
        "tenants": [{"id": tenant, "name": tenant_label, "timezone": world.timezone,
                     "policy_id": "receivables", "policy_version": 2}],
        "policies": [
            {"tenant": tenant, "id": "receivables", "version": 1, "threshold": old,
             "exclude_disputed": 1, "requires_approval": 0},
            {"tenant": tenant, "id": "receivables", "version": 2, "threshold": world.threshold,
             "exclude_disputed": 1, "requires_approval": 0},
        ],
        "exclusions": [
            {"tenant": tenant, "policy_id": "receivables", "version": v, "subject": "customer", "status": "closed"}
            for v in (1, 2)
        ],
        "users": [{"tenant": tenant, "id": "analyst", "role": "analyst"},
                  {"tenant": tenant, "id": "viewer", "role": "viewer"}],
        "customers": [], "documents": [], "contracts": [], "clauses": [], "amendments": [],
        "invoices": [], "payments": [], "disputes": [], "tickets": [],
    }
    return world


def add_customer(world: World, namer: Namer, name: str | None = None, aliases=()):
    name = name or namer.company()
    cid = _slug(name.split()[0] + "-" + name.split()[1])[:40] + "-" + namer.code(2).lower()
    world.fixture["customers"].append({"tenant": world.tenant, "id": cid, "name": name, "status": "active",
                                       "aliases": sorted({name.split()[0], name, *aliases})})
    return cid, name


def add_contract(world, namer, rng, cid, *, net_days, evidenced=True, amendments=(), decoy_draft=False):
    code = namer.code()
    contract_id = f"C-{code}"
    doc = f"D-{code}" if evidenced else None
    if doc:
        world.fixture["documents"].append({
            "tenant": world.tenant, "id": doc, "title": f"Master services agreement {code}", "kind": "contract",
            "text": f"Payment is due {net_days} calendar days after the invoice issue date."})
    world.fixture["contracts"].append({
        "tenant": world.tenant, "id": contract_id, "customer": cid, "status": "signed",
        "signed_at": "2026-01-05T17:00:00.000000+00:00", "issue_from": "2026-01-05", "issue_to": None,
        "document": doc})
    world.fixture["clauses"].append({"tenant": world.tenant, "contract": contract_id, "net_days": net_days,
                                     "document": doc})
    contract = {"id": contract_id, "document": doc, "net_days": net_days, "amendments": []}
    specs = list(amendments)
    if decoy_draft:
        specs.append({"status": "draft", "net_days": net_days + 50, "priority": 50,
                      "issue_from": None, "issue_to": None, "activation_on": "2026-06-01"})
    for index, spec in enumerate(specs, start=1):
        adoc = f"D-{code}-AM{index}"
        signed = spec["status"] == "signed"
        window = ("for invoices issued from " + spec["issue_from"] if spec.get("issue_from") else
                  "for invoices issued on or after " + spec["activation_on"])
        if spec.get("issue_to"):
            window += " and before " + spec["issue_to"]
        text = (f"{'Signed amendment' if signed else 'Draft proposal, not executed'}: {window}, payment "
                f"is due {spec['net_days']} calendar days after issuance. Priority {spec['priority']}.")
        world.fixture["documents"].append({"tenant": world.tenant, "id": adoc,
                                           "title": f"{'Signed' if signed else 'Draft'} amendment {index} to {code}",
                                           "kind": "amendment", "text": text})
        row = {"tenant": world.tenant, "contract": contract_id, "id": f"A-{index}", "status": spec["status"],
               "signed_at": spec.get("signed_at", "2026-05-20T15:00:00.000000+00:00") if signed else None,
               "activation_on": spec["activation_on"], "issue_from": spec.get("issue_from"),
               "issue_to": spec.get("issue_to"), "priority": spec["priority"], "net_days": spec["net_days"],
               "document": adoc}
        world.fixture["amendments"].append(row)
        contract["amendments"].append(row)
    # A decoy contract for the same customer that governs no invoices.
    if rng.random() < 0.4:
        dcode = namer.code()
        dnet = rng.choice((10, 12, 14))
        world.fixture["documents"].append({
            "tenant": world.tenant, "id": f"D-{dcode}", "title": f"Ancillary services agreement {dcode}",
            "kind": "contract", "text": f"Payment is due {dnet} calendar days after the invoice issue date."})
        world.fixture["contracts"].append({
            "tenant": world.tenant, "id": f"C-{dcode}", "customer": cid, "status": "signed",
            "signed_at": "2026-01-05T17:00:00.000000+00:00", "issue_from": "2026-01-05", "issue_to": None,
            "document": f"D-{dcode}"})
        world.fixture["clauses"].append({"tenant": world.tenant, "contract": f"C-{dcode}", "net_days": dnet,
                                         "document": f"D-{dcode}"})
    return contract


def add_invoice(world, namer, rng, cid, contract, target, *, prefix, issue_window=None, exclude_disputed=True):
    """Create one invoice whose hand-calculated outcome matches ``target``."""
    base = target.split(":")[0]
    needs_window = target.endswith(":differs") or base == "ambiguous"
    for attempt in range(4000):
        if issue_window and (needs_window or attempt < 1500):
            start, end = issue_window
            issued_on = start + timedelta(days=rng.randrange((end - start).days))
        else:
            issued_on = world.business_date - timedelta(days=rng.randrange(5, 150))
        issued = datetime(issued_on.year, issued_on.month, issued_on.day, 16, 0, tzinfo=UTC)
        if issued >= world.sim - timedelta(days=1):
            continue
        total = rng.randrange(150, 25000) * 100
        stated = issued_on + timedelta(days=contract["net_days"])
        invoice = {"issued_at": _iso(issued), "due": stated.isoformat(), "total": total,
                   "payments": [], "dispute": False}
        if base == "paid":
            invoice["payments"] = [{"status": "posted", "amount": total,
                                    "posted_at": _iso(issued + timedelta(days=rng.randrange(5, 20)))}]
        elif base in ("eligible", "partial_paid", "existing_ticket", "disputed") and rng.random() < 0.5:
            part = rng.randrange(1, total // 100 // 2) * 100
            invoice["payments"] = [{"status": "posted", "amount": part,
                                    "posted_at": _iso(issued + timedelta(days=3))}]
            if rng.random() < 0.5:
                invoice["payments"].append({"status": "pending", "amount": rng.randrange(1, 50) * 100,
                                            "posted_at": None})
        if base == "disputed":
            invoice["dispute"] = True
        if any(p["posted_at"] and datetime.fromisoformat(p["posted_at"]) >= world.sim - timedelta(hours=1)
               for p in invoice["payments"]):
            continue
        outcome = hand_decision(invoice, contract, world, exclude_disputed=exclude_disputed)
        want = {
            "eligible": ("eligible", ["OVERDUE_WITH_OUTSTANDING_BALANCE"]),
            "existing_ticket": ("eligible", ["OVERDUE_WITH_OUTSTANDING_BALANCE"]),
            "paid": ("ineligible", ["NO_OUTSTANDING_BALANCE"]),
            "disputed": ("ineligible", ["ACTIVE_DISPUTE"]),
            "threshold": ("ineligible", ["OVERDUE_THRESHOLD_NOT_MET"]),
            "ambiguous": ("needs_review", ["AMBIGUOUS_AMENDMENT"]),
            "missing_evidence": ("needs_review", ["MISSING_TERM_EVIDENCE"]),
        }[base]
        if (outcome["decision"], outcome["reason_codes"]) != want:
            continue
        if target.endswith(":differs") and not outcome["stated_due_date_differs"]:
            continue
        break
    else:
        raise RuntimeError(f"Could not realise outcome {target}")
    inv = f"INV-{prefix}{rng.randrange(100, 999)}"
    while any(row["id"] == inv for row in world.fixture["invoices"]):
        inv = f"INV-{prefix}{rng.randrange(100, 999)}"
    world.fixture["invoices"].append({"tenant": world.tenant, "id": inv, "customer": cid,
                                      "contract": contract["id"], "issued_at": invoice["issued_at"],
                                      "due": invoice["due"], "total": invoice["total"]})
    for n, payment in enumerate(invoice["payments"], start=1):
        world.fixture["payments"].append({"tenant": world.tenant, "id": f"PMT-{inv[4:]}-{n}", "invoice": inv,
                                          "status": payment["status"], "amount": payment["amount"],
                                          "posted_at": payment["posted_at"]})
    if invoice["dispute"]:
        world.fixture["disputes"].append({"tenant": world.tenant, "id": f"DSP-{inv[4:]}", "invoice": inv,
                                          "opened_at": _iso(world.sim - timedelta(days=rng.randrange(3, 20))),
                                          "reason": rng.choice(("Customer disputes delivered quantity",
                                                                "Customer disputes the billed rate",
                                                                "Service credit under negotiation"))})
    ticket = None
    if base == "existing_ticket":
        ticket = f"TCK-{inv[4:]}"
        world.fixture["tickets"].append({"tenant": world.tenant, "id": ticket, "invoice": inv,
                                         "policy_id": "receivables", "version": 2, "actor": "analyst",
                                         "created_at": _iso(world.sim - timedelta(days=rng.randrange(2, 9)))})
    return inv, outcome, ticket


def amendment_world(world, rng, kind):
    """Return amendment specs plus the issue window they govern (or None)."""
    bd = world.business_date
    start = bd - timedelta(days=rng.randrange(110, 150))
    end = start + timedelta(days=rng.randrange(35, 60))
    signed = lambda net, prio, **kw: {"status": "signed", "net_days": net, "priority": prio,
                                      "issue_from": start.isoformat(), "issue_to": end.isoformat(),
                                      "activation_on": (start - timedelta(days=10)).isoformat(), **kw}
    if kind == "none":
        return [], None
    if kind == "single":
        return [signed(rng.choice((40, 45, 50, 60)), 10)], (start, end)
    if kind == "ambiguous":
        a, b = rng.sample((40, 45, 50, 60, 75), 2)
        return [signed(a, 10), signed(b, 10, signed_at="2026-05-22T15:00:00.000000+00:00")], (start, end)
    raise ValueError(kind)


# ---------------------------------------------------------------- family builders

def build_case(family: str, split: str, index: int, rng: random.Random, namer: Namer) -> dict:
    code = namer.code()
    world = new_world(rng, namer, code)
    prefix = code[:2]
    template = TEMPLATES[family][index % len(TEMPLATES[family])]
    decoy = rng.random() < 0.5
    profile = "enterprise_ops:v2" if family == "paged_review" else "enterprise_ops:v1"
    faults = []
    net = rng.choice((20, 30, 30, 45))

    if family in ("inventory", "paged_review"):
        cid, name = add_customer(world, namer)
        if decoy or family == "paged_review":
            # A second account that shares the short alias and contains the legal name.
            add_customer(world, namer, name=f"{name.rsplit(' ', 1)[0]} West {name.rsplit(' ', 1)[1]}",
                         aliases=(name,))
            world.fixture["customers"][-1]["id"] = cid + "-west"
        kind = rng.choice(("none", "single"))
        specs, window = amendment_world(world, rng, kind)
        contract = add_contract(world, namer, rng, cid, net_days=net, amendments=specs,
                                decoy_draft=rng.random() < 0.4)
        count = rng.randrange(3, 6) if family == "inventory" else rng.randrange(5, 8)
        targets = ["eligible", "threshold", rng.choice(("paid", "disputed")), "existing_ticket"]
        targets += [rng.choice(("eligible", "threshold", "paid", "disputed")) for _ in range(count - 4)]
        if family == "paged_review" and rng.random() < 0.4:
            amb_specs, amb_window = amendment_world(world, rng, "ambiguous")
            amb = add_contract(world, namer, rng, cid, net_days=net, amendments=amb_specs)
        else:
            amb = None
        rng.shuffle(targets)
        invoices, tickets = {}, {}
        for target in targets[:count]:
            use_window = window if window and rng.random() < 0.6 else None
            inv, outcome, ticket = add_invoice(world, namer, rng, cid, contract, target, prefix=prefix,
                                               issue_window=use_window)
            invoices[inv] = outcome
            if ticket:
                tickets[inv] = ticket
        if amb is not None:
            inv, outcome, _ = add_invoice(world, namer, rng, cid, amb, "ambiguous", prefix=prefix,
                                          issue_window=amb_window)
            invoices[inv] = outcome
        task = {"request": template.format(name=name), "tenant_id": world.tenant, "user_id": "analyst",
                "operation": "list_invoices" if family == "inventory" else "review_invoices",
                "customer_query": name}
        expectations = {
            "tenant_id": world.tenant, "customer_id": cid, "actor_id": "analyst",
            "new_tickets": {}, "existing_tickets": tickets, "require_customer_search_exhausted": True,
            "customer_query": name, "require_existing_actions": False,
        }
        if family == "inventory":
            expectations |= {"invoices": {}, "coverage_invoice_ids": sorted(invoices)}
        else:
            expectations |= {"invoices": invoices, "invoice_page_size": 3}
            review = sorted(i for i, o in invoices.items() if o["decision"] == "needs_review")
            if review:
                expectations |= {
                    "status": "needs_review" if len(review) == len(invoices) else "partial",
                    "review_required": True,
                    "missing_information": [{"code": "ASSESSMENT_NEEDS_REVIEW", "subject_id": i} for i in review],
                }
            if rng.random() < 0.5:
                faults.append({"tool_name": "invoice.search", "attempt": 1,
                               "kind": rng.choice(("TRANSIENT_FAILURE", "RATE_LIMITED"))})

    elif family in ("review", "conflict", "escalate_one"):
        cid, name = add_customer(world, namer)
        if decoy:
            add_customer(world, namer)
        if family == "conflict":
            target = "missing_evidence" if index % 3 == 0 else "ambiguous"
            if target == "ambiguous":
                specs, window = amendment_world(world, rng, "ambiguous")
                contract = add_contract(world, namer, rng, cid, net_days=net, amendments=specs)
            else:
                specs, window = [], None
                contract = add_contract(world, namer, rng, cid, net_days=net, evidenced=False)
        else:
            choices = (("eligible:differs", "eligible", "threshold", "paid", "disputed") if family == "review"
                       else ("eligible", "eligible:differs", "eligible", "threshold", "existing_ticket", "paid"))
            target = choices[index % len(choices)]
            specs, window = amendment_world(world, rng, "single" if target.endswith("differs") or rng.random() < 0.4
                                            else "none")
            contract = add_contract(world, namer, rng, cid, net_days=net, amendments=specs,
                                    decoy_draft=rng.random() < 0.5)
        # The target invoice plus one or two siblings that are out of scope.
        use_window = window if (window and (target.endswith("differs") or target == "ambiguous")) else None
        inv, outcome, ticket = add_invoice(world, namer, rng, cid, contract, target, prefix=prefix,
                                           issue_window=use_window, exclude_disputed=True)
        for _ in range(rng.randrange(1, 3)):
            sibling = ("missing_evidence" if target == "missing_evidence"
                       else rng.choice(("eligible", "threshold", "paid")))
            add_invoice(world, namer, rng, cid, contract, sibling, prefix=prefix)
        task = {"request": template.format(name=name, inv=inv), "tenant_id": world.tenant, "user_id": "analyst",
                "operation": "escalate" if family == "escalate_one" else "review_invoices",
                "customer_id": cid, "invoice_ids": [inv]}
        expectations = {"tenant_id": world.tenant, "customer_id": cid, "actor_id": "analyst",
                        "invoices": {inv: outcome}, "new_tickets": {},
                        "existing_tickets": {inv: ticket} if ticket else {}, "invoice_search_required": False}
        if family == "escalate_one":
            task["authorized_actions"] = ["create_ticket"]
            if outcome["decision"] == "eligible" and not ticket:
                expectations["new_tickets"] = {inv: {"status": "open",
                                                     "reason_code": "OVERDUE_WITH_OUTSTANDING_BALANCE"}}
        else:
            expectations["require_existing_actions"] = False
        if outcome["decision"] == "needs_review":
            expectations |= {"status": "needs_review", "review_required": True,
                             "missing_information": [{"code": "ASSESSMENT_NEEDS_REVIEW", "subject_id": inv}]}

    elif family == "escalate_account":
        cid, name = add_customer(world, namer)
        if decoy:
            add_customer(world, namer)
        specs, window = amendment_world(world, rng, rng.choice(("none", "single")))
        contract = add_contract(world, namer, rng, cid, net_days=net, amendments=specs,
                                decoy_draft=rng.random() < 0.4)
        targets = ["eligible", "disputed", rng.choice(("paid", "threshold"))]
        targets += rng.sample(["eligible", "existing_ticket", "threshold", "paid"], rng.randrange(1, 3))
        rng.shuffle(targets)
        invoices, tickets, new = {}, {}, {}
        for target in targets:
            use_window = window if window and rng.random() < 0.6 else None
            inv, outcome, ticket = add_invoice(world, namer, rng, cid, contract, target, prefix=prefix,
                                               issue_window=use_window)
            invoices[inv] = outcome
            if ticket:
                tickets[inv] = ticket
            elif outcome["decision"] == "eligible":
                new[inv] = {"status": "open", "reason_code": "OVERDUE_WITH_OUTSTANDING_BALANCE"}
        task = {"request": template.format(name=name, days=world.threshold), "tenant_id": world.tenant,
                "user_id": "analyst", "operation": "escalate", "customer_id": cid,
                "authorized_actions": ["create_ticket"], "threshold_days": world.threshold,
                "exclude_disputed": True}
        expectations = {"tenant_id": world.tenant, "customer_id": cid, "actor_id": "analyst",
                        "invoices": invoices, "new_tickets": new, "existing_tickets": tickets}
        if rng.random() < 0.3:
            faults.append({"tool_name": "invoice.search", "attempt": 1, "kind": "TRANSIENT_FAILURE"})
    else:
        raise ValueError(family)

    task["request"] = task["request"].replace("..", ".")
    case_id = f"cmp-{SPLIT_CODE[split]}{index:02d}-{code.lower()}"
    return {"id": case_id, "lineage": f"cmp-{world.tenant}", "profile": profile, "task": task,
            "faults": faults, "expectations": expectations, "_family": family, "_world": world}


# ---------------------------------------------------------------- SQLite materialisation

def write_snapshot(world: World, path: Path) -> str:
    """Seed with the same statements as the frozen fixture loader, from an in-memory fixture."""
    f = world.fixture
    with closing(connect(path)) as connection:
        initialize(connection)
        with transaction(connection, write=True):
            connection.executemany(
                "INSERT INTO tenants (id, name, timezone, revision) VALUES (:id, :name, :timezone, 1)", f["tenants"])
            connection.executemany(
                "INSERT INTO escalation_policies (tenant_id, id, version, threshold_days, exclude_disputed, "
                "requires_approval) VALUES (:tenant, :id, :version, :threshold, :exclude_disputed, :requires_approval)",
                f["policies"])
            connection.executemany(
                "UPDATE tenants SET default_policy_id = :policy_id, default_policy_version = :policy_version "
                "WHERE id = :id", f["tenants"])
            connection.executemany(
                "INSERT INTO policy_exclusions (tenant_id, policy_id, policy_version, subject, status) "
                "VALUES (:tenant, :policy_id, :version, :subject, :status)", f["exclusions"])
            connection.executemany("INSERT INTO users (tenant_id, id, role) VALUES (:tenant, :id, :role)", f["users"])
            connection.executemany(
                "INSERT INTO customers (tenant_id, id, legal_name, status) VALUES (:tenant, :id, :name, :status)",
                f["customers"])
            connection.executemany(
                "INSERT INTO customer_aliases (tenant_id, customer_id, normalized_alias) VALUES (?, ?, ?)",
                ((c["tenant"], c["id"], " ".join(a.casefold().split())) for c in f["customers"] for a in c["aliases"]))
            for document in f["documents"]:
                text = document["text"]
                connection.execute(
                    "INSERT INTO document_versions (tenant_id, id, version, title, kind, source_uri, content, "
                    "content_hash) VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                    (document["tenant"], document["id"], document["title"], document["kind"],
                     f"fixture://comparative/{document['tenant']}/{document['id']}", text,
                     sha256(text.encode()).hexdigest()))
                connection.execute(
                    "INSERT INTO document_spans (tenant_id, document_id, document_version, id, start_offset, "
                    "end_offset) VALUES (?, ?, 1, 'terms', 0, ?)", (document["tenant"], document["id"], len(text)))
            connection.executemany(
                "INSERT INTO contracts (tenant_id, id, customer_id, status, signed_at, issue_from, issue_to, "
                "source_document_id, source_document_version) VALUES (:tenant, :id, :customer, :status, :signed_at, "
                ":issue_from, :issue_to, :document, CASE WHEN :document IS NOT NULL THEN 1 END)", f["contracts"])
            connection.executemany(
                "INSERT INTO payment_clauses (tenant_id, contract_id, id, net_days, grace_days, next_weekday, "
                "document_id, document_version, span_id) VALUES (:tenant, :contract, 'payment_terms', :net_days, 0, 0, "
                ":document, CASE WHEN :document IS NOT NULL THEN 1 END, "
                "CASE WHEN :document IS NOT NULL THEN 'terms' END)", f["clauses"])
            connection.executemany(
                "INSERT INTO amendments (tenant_id, contract_id, id, status, signed_at, activation_on, issue_from, "
                "issue_to, priority, source_document_id, source_document_version) VALUES (:tenant, :contract, :id, "
                ":status, :signed_at, :activation_on, :issue_from, :issue_to, :priority, :document, "
                "CASE WHEN :document IS NOT NULL THEN 1 END)", f["amendments"])
            connection.executemany(
                "INSERT INTO amendment_changes (tenant_id, contract_id, amendment_id, clause_id, field, value, "
                "document_id, document_version, span_id) VALUES (:tenant, :contract, :id, 'payment_terms', "
                "'net_days', :net_days, :document, CASE WHEN :document IS NOT NULL THEN 1 END, "
                "CASE WHEN :document IS NOT NULL THEN 'terms' END)", f["amendments"])
            connection.executemany(
                "INSERT INTO invoices (tenant_id, id, customer_id, contract_id, issued_at, stated_due_date, currency, "
                "total_minor, status) VALUES (:tenant, :id, :customer, :contract, :issued_at, :due, 'USD', :total, "
                "'issued')", f["invoices"])
            connection.executemany(
                "INSERT INTO payments (tenant_id, id, invoice_id, kind, status, amount_minor, currency, posted_at) "
                "VALUES (:tenant, :id, :invoice, 'payment', :status, :amount, 'USD', :posted_at)", f["payments"])
            connection.executemany(
                "INSERT INTO disputes (tenant_id, id, invoice_id, opened_at, reason) "
                "VALUES (:tenant, :id, :invoice, :opened_at, :reason)", f["disputes"])
            connection.executemany(
                "INSERT INTO escalation_tickets (tenant_id, id, invoice_id, policy_id, policy_version, status, "
                "reason_code, created_by, created_at) VALUES (:tenant, :id, :invoice, :policy_id, :version, 'open', "
                "'OVERDUE_WITH_OUTSTANDING_BALANCE', :actor, :created_at)", f["tickets"])
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def cross_check(case: dict, path: Path) -> None:
    """Abort unless the frozen policy engine agrees with every hand-calculated decision."""
    world = case["_world"]
    task = case["task"]
    restrictions = EscalationRestrictions(task.get("threshold_days"), task.get("exclude_disputed", False))
    clock = FrozenClock(world.sim)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        for invoice_id, wanted in case["expectations"]["invoices"].items():
            got = load_assessment(connection, world.tenant, invoice_id, clock, restrictions)
            refs = sorted({json.loads(ref)[1] for ref in got.terms.source_refs})
            observed = {
                "decision": got.status, "reason_codes": sorted(got.reason_codes),
                "effective_due_date": got.terms.effective_due_date.isoformat() if got.terms.effective_due_date else None,
                "days_past_due": got.days_past_due, "outstanding_minor": got.balance.outstanding_minor,
                "evidence": refs,
            }
            expected = {k: wanted[k] for k in ("decision", "reason_codes", "effective_due_date", "days_past_due",
                                                "outstanding_minor")}
            expected["evidence"] = [row["source_id"] for row in wanted["evidence_sources"]]
            if observed != expected:
                raise RuntimeError(f"Policy engine disagrees for {case['id']} {invoice_id}: {observed} != {expected}")
        connection.rollback()


def generate(root: Path, seed: int = GENERATOR_SEED) -> dict:
    rng = random.Random(seed)
    namer = Namer(rng)
    if root.exists():
        raise SystemExit(f"{root} already exists")
    root.mkdir(mode=0o700, parents=True)
    index = {"generator_seed": seed, "splits": {}}
    for split, allocation in ALLOCATION.items():
        directory = root / split
        directory.mkdir(mode=0o700)
        cases = []
        families = [family for family in FAMILIES for _ in range(allocation[family])]
        for number, family in enumerate(families, start=1):
            case = build_case(family, split, number, rng, namer)
            snapshot = directory / f"{case['id']}.sqlite3"
            digest = write_snapshot(case["_world"], snapshot)
            snapshot.chmod(0o600)
            cross_check(case, snapshot)
            cases.append(case | {"snapshot": {"filename": snapshot.name, "sha256": digest,
                                              "simulation_time": case["_world"].sim.isoformat()}})
        manifest = {"schema_version": 1, "split": "heldout",
                    "cases": [{k: v for k, v in case.items() if not k.startswith("_")} for case in cases]}
        path = directory / "manifest.json"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(manifest, stream, indent=1, sort_keys=True)
        with path.open("rb") as stream:
            manifest_digest = file_digest(stream, "sha256").hexdigest()
        index["splits"][split] = {
            "manifest_sha256": manifest_digest,
            "families": {case["id"]: case["_family"] for case in cases},
        }
    # Family labels are analysis metadata; they stay with the custodian copy.
    with os.fdopen(os.open(root / "index.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(index, stream, indent=1, sort_keys=True)
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=GENERATOR_SEED)
    args = parser.parse_args()
    os.umask(0o077)
    index = generate(args.out, args.seed)
    print(json.dumps({split: data["manifest_sha256"] for split, data in index["splits"].items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
