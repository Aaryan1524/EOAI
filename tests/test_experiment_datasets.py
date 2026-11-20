import json
import os
from contextlib import closing
from dataclasses import replace
from hashlib import sha256

import pytest

from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.sandbox import seed_snapshot
from enterprise_ops.storage.sqlite import connect
from experiments.eoai_eval import datasets


def test_public_splits_preserve_fixture_lineage_and_catalog_assignments():
    adaptation = datasets.load_adaptation()
    gate = datasets.load_gate()
    datasets.validate_disjoint(adaptation, gate)
    assert adaptation.split == "adaptation"
    assert gate.split == "gate"
    assert adaptation.digest == datasets.load_adaptation().digest
    assert gate.digest == datasets.load_gate().digest
    assert adaptation.digest != gate.digest
    assert {case.lineage for case in adaptation.cases}.isdisjoint(case.lineage for case in gate.cases)

    exact_acme = next(case for case in adaptation.cases if case.id == "acme-account-escalation")
    paged_acme = next(case for case in adaptation.cases if case.id == "acme-paged-v2")
    harborline, = gate.cases
    assert exact_acme.task == load_task("acme-account-escalation", authorize_writes=True).task
    assert exact_acme.expectations["existing_tickets"] == {"INV-106": "TCK-901"}
    assert paged_acme.profile == harborline.profile == "enterprise_ops:v2"
    assert len(paged_acme.expectations["invoices"]) == 7
    assert harborline.task == load_task("compound-escalation", authorize_writes=True).task
    assert len(harborline.expectations["invoices"]) == 6
    assert harborline.faults.steps == load_task("compound-escalation").faults.steps


def test_adaptation_reads_only_its_manifest(tmp_path, monkeypatch):
    data = datasets._DATASETS / "adaptation.json"
    (tmp_path / data.name).write_bytes(data.read_bytes())
    monkeypatch.setattr(datasets, "_DATASETS", tmp_path)
    assert datasets.load_adaptation().split == "adaptation"
    with pytest.raises(datasets.DatasetError, match="Invalid or unreadable"):
        datasets.load_gate()


def test_duplicate_assignment_rejected_after_renaming_and_text_normalization():
    adaptation = datasets.load_adaptation()
    first, *_ = adaptation.cases
    alias = replace(
        first, id="alias", catalog_task=None, lineage="other", snapshot_fingerprint="different",
        task=replace(first.task, id="alias", request=first.task.request.upper().replace(" ", "  ")),
    )
    with pytest.raises(datasets.DatasetError, match="Cross-split overlap in task assignment"):
        datasets.validate_disjoint(adaptation, replace(adaptation, split="gate", cases=(alias,)))
    with pytest.raises(datasets.DatasetError, match="Duplicate case ID"):
        datasets.validate_disjoint(replace(adaptation, cases=(first, first)))


def test_renaming_snapshot_and_assignment_cannot_hide_shared_lineage():
    adaptation = datasets.load_adaptation()
    gate = datasets.load_gate()
    (case,) = gate.cases
    baseline = replace(case, lineage=adaptation.cases[0].lineage)
    with pytest.raises(datasets.DatasetError, match="Cross-split overlap in fixture lineage"):
        datasets.validate_disjoint(adaptation, replace(gate, cases=(baseline,)))


def test_heldout_schema_has_no_private_suite_examples():
    schema = json.loads((datasets._DATASETS / "heldout.schema.json").read_bytes())
    assert schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema.pop("title") == "EOAI evaluator-only held-out suite"
    current = datasets._HeldoutManifest.model_json_schema()
    current.pop("title")
    assert schema == current
    assert schema["properties"]["split"]["const"] == "heldout"
    assert current["$defs"]["_HeldoutCase"]["additionalProperties"] is False


@pytest.fixture
def demo_heldout(tmp_path):
    workspace = tmp_path / "improvement"
    workspace.mkdir()
    private = tmp_path / "custodian"
    private.mkdir(mode=0o700)
    seed = seed_snapshot(private, "demo", scenario="baseline")
    with closing(connect(seed.path)) as connection:
        connection.execute(
            "UPDATE customers SET legal_name = 'Demo private name' WHERE tenant_id = ? AND id = ?",
            ("northstar_property", "acme"),
        )
    path = private / "suite.json"
    data = {
        "schema_version": 1,
        "split": "heldout",
        "cases": [{
            "id": "demo-heldout",
            "lineage": "demo-only",
            "profile": "enterprise_ops:v1",
            "task": {
                "request": "List the invoices for Demo private name.",
                "tenant_id": "northstar_property",
                "user_id": "analyst",
                "operation": "list_invoices",
                "customer_query": "Demo private name",
            },
            "snapshot": {
                "filename": seed.path.name,
                "sha256": sha256(seed.path.read_bytes()).hexdigest(),
                "simulation_time": seed.clock.instant.isoformat(),
            },
            "expectations": {
                "tenant_id": "northstar_property",
                "customer_id": "acme",
                "actor_id": "analyst",
                "invoices": {},
                "coverage_invoice_ids": ["INV-104", "INV-105", "INV-106", "INV-107"],
                "new_tickets": {},
                "existing_tickets": {"INV-106": "TCK-901"},
                "require_existing_actions": False,
            },
        }],
    }
    _write_manifest(path, data)
    return workspace, path, data


def _write_manifest(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def test_two_directories_do_not_allow_private_storage_inside_improvement(demo_heldout):
    _, path, _ = demo_heldout
    with pytest.raises(datasets.DatasetError, match="outside the improvement workspace"):
        datasets.load_heldout(path, improvement_root=path.parent)


def test_git_storage_cannot_be_private(demo_heldout):
    workspace, path, _ = demo_heldout
    (path.parent / ".git").mkdir()
    with pytest.raises(datasets.DatasetError, match="outside Git checkouts"):
        datasets.load_heldout(path, improvement_root=workspace)


def test_heldout_public_projection_contains_only_model_inputs(demo_heldout):
    workspace, path, _ = demo_heldout
    suite = datasets.load_heldout(path, improvement_root=workspace)
    (case,) = suite.cases
    assert suite.split == "heldout"
    assert case.catalog_task is None and case.scenario is None
    assert case.snapshot.path == path.parent / "demo.sqlite3"
    assert case.public_input() == {
        "request": "List the invoices for Demo private name.",
        "operation": "list_invoices",
        "customer_query": "Demo private name",
        "customer_id": None,
        "invoice_ids": None,
        "authorized_actions": [],
    }
    assert "TCK-901" not in repr(case)
    assert "TCK-901" not in str(case.public_input())
    assert "demo.sqlite3" not in repr(case)


@pytest.mark.parametrize("bad_file", ["manifest", "snapshot", "symlink", "hardlink"])
def test_heldout_requires_private_regular_files(demo_heldout, bad_file):
    workspace, path, data = demo_heldout
    if bad_file == "manifest":
        path.chmod(0o644)
    else:
        snapshot = path.parent / data["cases"][0]["snapshot"]["filename"]
        if bad_file == "snapshot":
            snapshot.chmod(0o644)
        elif bad_file == "symlink":
            original = snapshot.rename(snapshot.with_suffix(".original"))
            snapshot.symlink_to(original)
        else:
            os.link(snapshot, snapshot.with_suffix(".other"))
    with pytest.raises(datasets.DatasetError, match="private|Invalid held-out case"):
        datasets.load_heldout(path, improvement_root=workspace)


def test_heldout_requires_private_directory(demo_heldout):
    workspace, path, _ = demo_heldout
    path.parent.chmod(0o755)
    with pytest.raises(datasets.DatasetError, match="custodian-owned and private"):
        datasets.load_heldout(path, improvement_root=workspace)


def test_heldout_rejects_digest_mismatch(demo_heldout):
    workspace, path, data = demo_heldout
    data["cases"][0]["snapshot"]["sha256"] = "0" * 64
    _write_manifest(path, data)
    with pytest.raises(datasets.DatasetError, match="Invalid held-out case or snapshot"):
        datasets.load_heldout(path, improvement_root=workspace)


def test_external_copy_of_public_fixture_rejected(demo_heldout):
    workspace, path, data = demo_heldout
    snapshot = seed_snapshot(path.parent, "copied-but-renamed", scenario="baseline")
    data["cases"][0]["snapshot"].update(filename=snapshot.path.name, sha256=snapshot.digest)
    _write_manifest(path, data)
    with pytest.raises(datasets.DatasetError, match="Cross-split overlap in snapshot contents"):
        datasets.load_heldout(path, improvement_root=workspace)


def test_private_manifest_rejects_extra_agent_fields(demo_heldout):
    workspace, path, data = demo_heldout
    data["cases"][0]["task"]["gold_case_label"] = "private-marker"
    _write_manifest(path, data)
    with pytest.raises(datasets.DatasetError, match="Invalid or unreadable") as error:
        datasets.load_heldout(path, improvement_root=workspace)
    assert "private-marker" not in str(error.value)
