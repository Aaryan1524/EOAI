import json

import pytest

from enterprise_ops.agent.model import ModelTurn, ToolCall
from enterprise_ops.agent.runner import RunLimits
from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.sandbox import seed_snapshot
from experiments.eoai_eval.execution import TrialConfig, fixed_conditions, load_variant, run_trial, sha_file


def test_variant_allowlist_and_digest(tmp_path):
    bundle = tmp_path / "candidate.json"
    content = {"instructions": "Instructions", "skills": "", "memory": ""}
    bundle.write_text(json.dumps(content), encoding="utf-8")
    first = load_variant(bundle)

    bundle.write_text(json.dumps(dict(reversed(list(content.items())))), encoding="utf-8")
    assert load_variant(bundle).digest == first.digest

    for changed in content:
        bundle.write_text(json.dumps(content | {changed: "Changed"}), encoding="utf-8")
        assert load_variant(bundle).digest != first.digest

    for forbidden in ("tool_profile", "deadline_seconds", "snapshot", "runner", "model"):
        bundle.write_text(json.dumps(content | {forbidden: "changed"}), encoding="utf-8")
        with pytest.raises(ValueError, match="must contain only"):
            load_variant(bundle)

    bundle.write_text('{"instructions":"a","instructions":"b","skills":"","memory":""}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        load_variant(bundle)


def test_fixed_conditions_compare_harness_only():
    original = {
        "source": {"execution_sha256": "original"},
        "runtime": {"python": "3.12"},
        "tool_versions": {"invoice.search": "v1"},
        "run": {
            "run_id": "first", "sandbox_id": "fresh", "harness": {"version": "v3"},
            "limits": {"tool_calls": 35}, "profile": "enterprise_ops:v1",
        },
    }
    variant = original | {
        "run": original["run"] | {
            "run_id": "second", "sandbox_id": "fresh-again", "harness": {"version": "experiment"},
        }
    }
    assert fixed_conditions(original) == fixed_conditions(variant)

    for key in ("limits", "profile"):
        changed = variant | {"run": variant["run"] | {key: "changed"}}
        assert fixed_conditions(original) != fixed_conditions(changed)
    assert fixed_conditions(original) != fixed_conditions(variant | {"source": {"execution_sha256": "different"}})
    assert fixed_conditions(original) != fixed_conditions(variant | {"tool_versions": {"invoice.search": "v2"}})


def test_trial_preserves_reset_and_auditable_provenance(tmp_path):
    class ScriptModel:
        provider = "script"
        model_id = "constant"
        settings = {"seed": 0}

        def model_digest(self):
            return "a" * 64

        def turn(self, **kwargs):
            if len(kwargs["transcript"]) == 1:
                return ModelTurn(calls=(ToolCall("clock", "clock.now", "{}"),))
            return ModelTurn(final='{"customer_id":null,"review_reason":"MISSING_INFORMATION"}')

    entry = load_task("acme-list")
    snapshot = seed_snapshot(tmp_path / "snapshots", "source")
    config = TrialConfig("a" * 64, RunLimits(deadline_seconds=30), {"commit": "head"}, {"python": "test"})
    trials = [
        run_trial(config, ScriptModel(), load_variant(None), snapshot, entry.task, entry.faults,
                  run_id=f"trial-{number}", root=tmp_path, scenario=entry.scenario)
        for number in (1, 2)
    ]
    manifests = [json.loads((trial.trace_path / "experiment.json").read_text()) for trial in trials]
    assert fixed_conditions(manifests[0]) == fixed_conditions(manifests[1])
    assert trials[0].sandbox.path != trials[1].sandbox.path
    assert sha_file(snapshot.path) == snapshot.digest
    for trial, manifest in zip(trials, manifests, strict=True):
        assert manifest["run"]["snapshot_digest"] == snapshot.digest
        assert manifest["run"]["task_id"] == entry.task.id
        assert manifest["tool_versions"]["clock.now"] == "v1"
        assert manifest["elapsed_seconds"] >= 0
        assert manifest["artifacts"]["trace.jsonl"] == sha_file(trial.trace_path / "trace.jsonl")
        assert manifest["artifacts"]["run.sqlite3"] == sha_file(trial.sandbox.path)
