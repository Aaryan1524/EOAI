import json
from types import SimpleNamespace

import pytest

from enterprise_ops.agent.model import ModelTurn, ToolCall
from enterprise_ops.agent.runner import RunLimits
from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.sandbox import seed_snapshot
from experiments.eoai_eval.execution import (
    TrialConfig, fixed_conditions, load_variant, run_trial, sha_file, verify_trial_artifacts,
)
from experiments.eoai_eval import __main__ as evaluation


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
        verify_trial_artifacts(trial.trace_path, trial.sandbox.path, manifest)
        assert manifest["run"]["snapshot_digest"] == snapshot.digest
        assert manifest["run"]["task_id"] == entry.task.id
        assert manifest["tool_versions"]["clock.now"] == "v1"
        assert manifest["elapsed_seconds"] >= 0
        assert manifest["artifacts"]["trace.jsonl"] == sha_file(trial.trace_path / "trace.jsonl")
        assert manifest["artifacts"]["run.sqlite3"] == sha_file(trial.sandbox.path)
    with (trials[0].trace_path / "trace.jsonl").open("a") as trace:
        trace.write("\n")
    with pytest.raises(ValueError, match="artifact digests"):
        verify_trial_artifacts(trials[0].trace_path, trials[0].sandbox.path, manifests[0])


@pytest.mark.parametrize("different_limits", [False, True])
def test_heldout_uses_one_fixed_configuration_and_keeps_output_private(
    tmp_path, monkeypatch, capsys, different_limits,
):
    private = tmp_path / "vault"
    private.mkdir(mode=0o700)
    bundle = tmp_path / "candidate.json"
    bundle.write_text('{"instructions":"Candidate","skills":"","memory":""}')
    entry = load_task("acme-list")
    case = SimpleNamespace(
        id="sealed", task=entry.task, snapshot=object(), faults=entry.faults, scenario=None,
        expectations={"private": "answer-canary"},
    )
    suite = SimpleNamespace(cases=(case,), digest="suite")
    monkeypatch.setattr(evaluation, "load_heldout", lambda *args, **kwargs: suite)
    monkeypatch.setattr(evaluation, "validate_disjoint", lambda *args: None)
    source = {"commit": "commit", "execution_sha256": "code", "evaluator_sha256": "evaluator",
              "tracked_changes": False}
    monkeypatch.setattr(evaluation, "source_identity", lambda: source)
    monkeypatch.setattr(evaluation, "runtime_identity", lambda model: {})
    monkeypatch.setattr(evaluation, "verify_trial_artifacts", lambda *args: None)
    observations = []

    def run(config, model, harness, snapshot, task, faults, *, run_id, root, scenario):
        assert task is entry.task
        observations.append((config, harness.digest, snapshot))
        trace = root / run_id
        trace.mkdir()
        (trace / "experiment.json").write_text(json.dumps({
            "source": config.source, "runtime": config.runtime, "tool_versions": {},
            "run": {"limits": 60 if different_limits and len(observations) == 2 else 180},
        }))
        return SimpleNamespace(trace_path=trace, sandbox=SimpleNamespace(path=tmp_path / "run.sqlite3"))

    monkeypatch.setattr(evaluation, "run_trial", run)
    monkeypatch.setattr(evaluation, "score", lambda *args: {"passed": True, "semantic_outcome": {}})
    case.snapshot = SimpleNamespace(path=tmp_path / "source.sqlite3")
    args = SimpleNamespace(
        split="heldout", suite=private / "manifest.json", improvement_root=tmp_path / "improvement",
        suite_digest=suite.digest, out=private / "results", case=None, commit="commit",
        execution_sha256="code", evaluator_sha256="evaluator", trials=1, harness=bundle,
        harness_digest=load_variant(bundle).digest, model="qwen3:4b-instruct", model_digest="a" * 64,
        endpoint="http://127.0.0.1:11434", deadline=180,
    )
    if different_limits:
        with pytest.raises(ValueError, match="Fixed experiment conditions changed"):
            evaluation._evaluate(args)
        assert not (args.out / "evaluation.json").exists()
    else:
        evaluation._evaluate(args)
        assert set(json.loads((args.out / "evaluation.json").read_text())["cases"][0]["arms"]) == {
            "baseline", "candidate",
        }
    assert observations[0][0] == observations[1][0]
    assert observations[0][1] != observations[1][1]
    assert observations[0][2] is observations[1][2]
    assert capsys.readouterr() == ("", "")
