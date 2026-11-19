from contextlib import closing

import pytest
from pydantic import ValidationError

from enterprise_ops.domain.entities import Principal
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, seed_snapshot
from enterprise_ops.storage.repository import transition_payment
from enterprise_ops.storage.sqlite import connect
from enterprise_ops.tools.gateway import TOOLS, ToolGateway


NORTH = Principal("northstar_property", "analyst")


@pytest.fixture
def sandboxes(tmp_path):
    return {
        version: seed_snapshot(
            tmp_path / "snapshots", f"baseline-{version}", profile=f"enterprise_ops:{version}",
        ).clone(tmp_path / "runs", f"workflow-{version}")
        for version in ("v1", "v2")
    }


def test_snapshot_profile_controls_declarations_and_schema_version(sandboxes):
    v1, v2 = (ToolGateway(sandboxes[version], NORTH) for version in ("v1", "v2"))
    first, second = (
        {tool["name"]: tool for tool in gateway.declarations()}
        for gateway in (v1, v2)
    )
    assert first.keys() == second.keys() == TOOLS.keys()
    for name, declaration in first.items():
        assert declaration["schema_version"] == "v1"
        assert second[name]["schema_version"] == "v2"
        assert declaration["result"] == second[name]["result"]
        assert declaration["arguments"] == TOOLS[name].arguments.model_json_schema()
        if name != "invoice.search":
            assert declaration["arguments"] == second[name]["arguments"]

    invoice_args = second["invoice.search"]["arguments"]
    assert set(invoice_args["properties"]) == {"account_ref", "page_size", "page_cursor", "statuses"}
    assert invoice_args["required"] == ["account_ref"]
    assert invoice_args["additionalProperties"] is False
    for gateway, current, other in ((v1, "v1", "v2"), (v2, "v2", "v1")):
        for response in (gateway.invoke("clock.now", {}), gateway.invoke("clock.now", {}, schema_version=current)):
            assert response.ok
            assert (response.meta.profile, response.meta.schema_version) == (f"enterprise_ops:{current}", current)
        for version in (other, "", "v3", None):
            response = gateway.invoke("clock.now", {}, schema_version=version)
            assert response.error.code == "SCHEMA_MISMATCH"
            assert response.meta.schema_version == current


def test_invoice_search_rejects_other_profile_fields_and_normalizes_valid_input(sandboxes):
    v1, v2 = (ToolGateway(sandboxes[version], NORTH) for version in ("v1", "v2"))
    assert v1.invoke("invoice.search", {"account_ref": "acme"}).error.code == "VALIDATION_ERROR"
    for arguments in (
        {"customer_id": "acme"},
        {"account_ref": "acme", "customer_id": "acme"},
        {"account_ref": "acme", "limit": 2},
        {"account_ref": "acme", "cursor": "cursor"},
        {"account_ref": "acme", "page_size": True},
        {"account_ref": "acme", "statuses": ["paid"]},
    ):
        assert v2.invoke("invoice.search", arguments).error.code == "VALIDATION_ERROR"
        with pytest.raises(ValidationError):
            v2.canonical_arguments("invoice.search", arguments)

    defaults = {"customer_id": "acme", "limit": 10, "cursor": None, "statuses": None}
    assert v1.canonical_arguments("invoice.search", {"customer_id": "acme"}) == defaults
    assert v2.canonical_arguments("invoice.search", {"account_ref": "acme"}) == defaults
    assert v2.canonical_arguments("invoice.search", {
        "account_ref": "acme", "page_size": 2, "page_cursor": "cursor", "statuses": ["issued"],
    }) == {"customer_id": "acme", "limit": 2, "cursor": "cursor", "statuses": ["issued"]}


def test_v2_invoice_search_paginates_through_faults_and_rejects_invalid_cursors(sandboxes):
    v2_sandbox = sandboxes["v2"]
    v2 = ToolGateway(v2_sandbox, NORTH, faults=FaultPlan((
        FaultStep("invoice.search", 1, FaultKind.RATE_LIMITED),
        FaultStep("invoice.search", 3, FaultKind.EMPTY_PAGE),
    )))
    args = {"account_ref": "acme", "page_size": 2, "statuses": ["issued"]}
    deadline = v2.invoke("invoice.search", args, timeout=0)
    assert deadline.error.code == "TRANSIENT_FAILURE"
    assert deadline.meta.schema_version == "v2"
    limited = v2.invoke("invoice.search", args)
    assert limited.error.code == "RATE_LIMITED"
    assert limited.error.retryable
    assert limited.meta.schema_version == "v2"

    first = v2.invoke("invoice.search", args)
    assert first.ok
    empty = v2.invoke("invoice.search", {**args, "page_cursor": first.data.next_cursor})
    assert empty.ok
    assert empty.data.items == []
    assert empty.data.next_cursor and empty.data.next_cursor != first.data.next_cursor
    last = v2.invoke("invoice.search", {**args, "page_cursor": empty.data.next_cursor})
    assert last.ok
    assert [row.id for row in first.data.items + last.data.items] == ["INV-104", "INV-105", "INV-106", "INV-107"]
    assert last.data.next_cursor is None
    assert first.meta.state_revision == empty.meta.state_revision == last.meta.state_revision
    assert first.meta.evidence_ids and empty.meta.evidence_ids and last.meta.evidence_ids

    cursor = first.data.next_cursor
    wrong_size = v2.invoke("invoice.search", {**args, "page_size": 1, "page_cursor": cursor})
    assert wrong_size.error.code == "VALIDATION_ERROR"
    v1 = ToolGateway(sandboxes["v1"], NORTH)
    v1_args = {"customer_id": "acme", "limit": 2, "statuses": ["issued"]}
    wrong_profile = v1.invoke("invoice.search", {**v1_args, "cursor": cursor})
    assert wrong_profile.error.code == "VALIDATION_ERROR"
    v1_first = v1.invoke("invoice.search", v1_args)
    assert v1_first.ok
    wrong_profile = v2.invoke("invoice.search", {**args, "page_cursor": v1_first.data.next_cursor})
    assert wrong_profile.error.code == "VALIDATION_ERROR"
    with closing(connect(v2_sandbox.path)) as db:
        transition_payment(db, Principal(NORTH.tenant_id, "simulator"), "PMT-2", "posted", v2_sandbox.clock)
    stale = v2.invoke("invoice.search", {**args, "page_cursor": cursor})
    assert stale.error.code == "STALE_REVISION"
