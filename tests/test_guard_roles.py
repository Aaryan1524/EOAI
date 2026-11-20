import json
import platform
import subprocess
import sys
from contextlib import contextmanager

import pytest

from enterprise_ops.agent.model import ModelError
from experiments.eoai_eval.isolation import role_command
from experiments.guard import roles


_DIGEST = "a" * 64
_REQUEST = {"strategy": "scope every rule", "incumbent": "existing policy", "regret": {}}


@pytest.fixture
def role_relay(monkeypatch, tmp_path):
    calls = []
    messages = []
    replies = {
        "models": [{"name": "qwen3:4b-instruct", "digest": "sha256:" + _DIGEST}],
        "chat": {
            "message": {
                "role": "assistant", "content": '{"playbook":"bounded rules"}', "thinking": "ignored",
            },
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 23,
            "eval_count": 7,
        },
    }

    @contextmanager
    def area():
        yield tmp_path

    def invoke(role, area, code, data, *, timeout):
        messages.append((role, data))
        return subprocess.run(
            [sys.executable, "-I", "-S", "-c", code], cwd=area, env={},
            input=json.dumps(data), capture_output=True, text=True, check=True, timeout=timeout,
        )

    def relay(model, method, path, timeout, body=None):
        assert timeout > 0
        calls.append((method, path, None if body is None else json.loads(body)))
        if (method, path) == ("GET", "/api/tags"):
            return {"models": replies["models"]}
        assert (method, path) == ("POST", "/api/chat")
        if isinstance(replies["chat"], Exception):
            raise replies["chat"]
        return replies["chat"]

    monkeypatch.setattr(roles, "role_area", area)
    monkeypatch.setattr(roles, "_invoke", invoke)
    monkeypatch.setattr(roles.OllamaModel, "_request", relay)
    return calls, messages, replies


def test_role_wire_uses_only_public_messages_and_assigns_stochastic_seeds(role_relay):
    calls, messages, _ = role_relay
    role_model = roles.RoleModel("sha256:" + _DIGEST, seed=41)
    for temperature in (0.65, 0.8, 0.0, 0.9):
        assert role_model("candidate-generator", "distill", "Only use public input.", _REQUEST, temperature) == {
            "playbook": "bounded rules",
        }

    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api/tags"), ("POST", "/api/chat"), ("GET", "/api/tags"),
    ] * 4
    sent = [body for _, _, body in calls if body is not None]
    assert sent[0] == {
        "model": "qwen3:4b-instruct",
        "messages": [
            {"role": "system", "content": "Only use public input."},
            {"role": "user", "content": json.dumps(_REQUEST, sort_keys=True, separators=(",", ":"))},
        ],
        "format": roles._role_format("distill", _REQUEST),
        "stream": False,
        "think": False,
        "options": {"num_ctx": 32768, "num_predict": 2048, "temperature": 0.65, "seed": 41},
    }
    assert [body["options"]["seed"] for body in sent] == [41, 42, 41, 43]
    assert [row["seed"] for row in role_model.metrics] == [41, 42, 41, 43]
    for row in role_model.metrics:
        assert row["status"] == "ok"
        assert (row["input_tokens"], row["output_tokens"]) == (23, 7)
        assert row["latency_seconds"] > 0
    assert len(messages) == 8
    for role, request in messages:
        assert role == "candidate-generator"
        if request["operation"] == "prepare":
            assert request.keys() == {"operation", "instruction", "payload"}
        else:
            assert request["message"].keys() == {"role", "content"}


@pytest.mark.parametrize("model_reply", [
    {"message": {"role": "assistant", "content": '{"x":1,"x":2}'}},
    {"message": {"role": "assistant", "content": '{"x":NaN}'}},
    {"message": {"role": "assistant", "content": '["x"]'}},
    {"message": {"role": "assistant", "content": '{"x":1}', "tool_calls": [{"function": "fake"}]}},
    {"message": {"role": "user", "content": '{"x":1}'}},
    {"done": False},
    {"done_reason": "length"},
])
def test_malformed_or_tool_using_response_fails_closed_and_keeps_tokens(role_relay, model_reply):
    _, _, replies = role_relay
    replies["chat"].update(model_reply)
    role_model = roles.RoleModel(_DIGEST)
    with pytest.raises(ValueError, match="Invalid JSON in role worker"):
        role_model("candidate-generator", "distill", "Propose a candidate.", _REQUEST, 0.65)
    row, = role_model.metrics
    assert row["status"] == "invalid_response"
    assert (row["input_tokens"], row["output_tokens"]) == (23, 7)


def test_role_call_rejects_unapproved_inputs_before_relay(role_relay):
    calls, messages, _ = role_relay
    role_model = roles.RoleModel(_DIGEST)
    for role, purpose, payload, temperature in (
        ("solver", "distill", _REQUEST, 0.65),
        ("reviewer", "distill", _REQUEST, 0.65),
        ("candidate-generator", "distill", dict(_REQUEST, private_answers=[]), 0.65),
        ("candidate-generator", "distill", _REQUEST, 1.0),
        ("candidate-generator", "distill", dict(_REQUEST, regret={"value": float("inf")}), 0.65),
    ):
        with pytest.raises(ValueError):
            role_model(role, purpose, "Propose a candidate.", payload, temperature)
    assert calls == messages == []
    assert all(row["status"] == "invalid_request" for row in role_model.metrics)


def test_model_digest_is_checked_before_and_after_inference(role_relay, monkeypatch):
    calls, messages, _ = role_relay
    role_model = roles.RoleModel(_DIGEST)
    digests = iter(("b" * 64, _DIGEST, "c" * 64))
    monkeypatch.setattr(roles.OllamaModel, "model_digest", lambda model, *, timeout: next(digests))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="digest changed"):
            role_model("candidate-generator", "distill", "Propose a candidate.", _REQUEST, 0.65)
    assert [path for _, path, _ in calls] == ["/api/chat"]
    assert [data["operation"] for _, data in messages] == ["prepare", "prepare"]
    assert [row["status"] for row in role_model.metrics] == ["model_identity_failed"] * 2
    assert [row["input_tokens"] for row in role_model.metrics] == [None, 23]


def test_provider_and_sandbox_failures_are_measured(role_relay, monkeypatch):
    _, _, replies = role_relay
    role_model = roles.RoleModel(_DIGEST)
    replies["chat"] = ModelError("Ollama request timed out")
    with pytest.raises(ModelError, match="timed out"):
        role_model("candidate-generator", "distill", "Propose a candidate.", _REQUEST, 0.65)

    def fail(role, area, code, data, *, timeout):
        raise subprocess.CalledProcessError(71, ["sandbox-exec"], stderr="output with sensitive paths")

    monkeypatch.setattr(roles, "_invoke", fail)
    with pytest.raises(RuntimeError, match="^Role sandbox invocation failed$"):
        role_model("candidate-generator", "distill", "Propose a candidate.", _REQUEST, 0.8)
    assert [row["status"] for row in role_model.metrics] == ["provider_failed", "sandbox_failed"]
    assert all(row["input_tokens"] is None for row in role_model.metrics)


@pytest.mark.parametrize("params", [
    {"seed": -1}, {"seed": 2**31}, {"seed": True}, {"timeout": float("nan")},
    {"timeout": 301}, {"context_window": 0}, {"context_window": 32769},
])
def test_transport_limits_are_bounded(params):
    with pytest.raises(ValueError):
        roles.RoleModel(_DIGEST, **params)


def test_relay_refuses_remote_ollama():
    with pytest.raises(ModelError, match="loopback"):
        roles.RoleModel(_DIGEST, endpoint="https://example.test")


def test_real_launcher_runs_role_worker_with_no_network_grant():
    if platform.system() != "Darwin":
        pytest.skip("Native macOS sandbox unavailable")
    with roles.role_area() as area:
        probe = subprocess.run(
            role_command("candidate-generator", area, "pass"), cwd=area, env={}, capture_output=True,
        )
        if probe.returncode == 71 and b"Operation not permitted" in probe.stderr:
            pytest.skip("Cannot install a nested sandbox inside this test host")
        assert probe.returncode == 0, probe.stderr
        result = roles._invoke("candidate-generator", area, roles._WORKER, {
            "operation": "prepare", "instruction": "Public instruction", "payload": _REQUEST,
        }).stdout
    assert json.loads(result)["result"][0] == {"role": "system", "content": "Public instruction"}
