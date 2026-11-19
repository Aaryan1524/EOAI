import json
import re
from dataclasses import dataclass
from unittest.mock import Mock

import pytest

from enterprise_ops.agent.model import ModelError, ModelTurn, OllamaModel


@dataclass
class Response:
    body: bytes
    status: int = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def read(self, size):
        return self.body[:size]


@pytest.fixture
def http(monkeypatch):
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr("enterprise_ops.agent.model.http.client.HTTPConnection", factory)
    return factory, client


def chat(client, *, calls=None, content="", **overrides):
    message = {"role": "assistant", "content": content, "thinking": "private reasoning"}
    if calls is not None:
        message["tool_calls"] = [{"function": call} for call in calls]
    payload = {
        "done": True,
        "message": message,
        "prompt_eval_count": 50,
        "eval_count": 24,
        **overrides,
    }
    client.getresponse.return_value = Response(json.dumps(payload).encode())


def declared(name):
    return {
        "name": name,
        "schema_version": "v1",
        "arguments": {"type": "object", "properties": {"query": {"type": "string"}}},
        "result": {"type": "object"},
    }


def turn(model=None, **overrides):
    return (model or OllamaModel("qwen3:4b-instruct")).turn(**{
        "instructions": "Select a tool or return a JSON object.",
        "transcript": [{"role": "user", "content": "Find Acme"}],
        "declarations": [declared("customer.search")],
        "timeout": 3.0,
        "max_output_tokens": 80,
        **overrides,
    })


def test_response_rejects_duplicate_keys_in_native_tool_arguments(http):
    _, client = http
    client.getresponse.return_value = Response(
        b'{"done":true,"message":{"role":"assistant","tool_calls":'
        b'[{"function":{"name":"customer_search","arguments":{"query":"Acme","query":"Other"}}}]}}'
    )
    with pytest.raises(ModelError, match="Invalid Ollama JSON response"):
        turn()


def test_pinned_model_rejects_changed_digest_before_inference(http):
    _, client = http
    model = OllamaModel("qwen3:4b-instruct")
    model.pin_model_digest("a" * 64)
    client.getresponse.return_value = Response(json.dumps({
        "models": [{"name": model.model_id, "digest": "b" * 64}],
    }).encode())
    with pytest.raises(ModelError, match="digest changed"):
        turn(model)
    assert [request.args[:2] for request in client.request.call_args_list] == [("GET", "/api/tags")]


def test_prefixed_inventory_digest_matches_its_bare_pin(http):
    _, client = http
    model = OllamaModel("qwen3:4b-instruct")
    expected = "a" * 64
    assert model.pin_model_digest("sha256:" + expected) == expected
    inventory = Response(json.dumps({"models": [{"name": model.model_id, "digest": "sha256:" + expected}]}).encode())
    chat(client, content="{}")
    client.getresponse.side_effect = [inventory, client.getresponse.return_value, inventory]
    assert turn(model).final == "{}"
    assert [request.args[:2] for request in client.request.call_args_list] == [
        ("GET", "/api/tags"), ("POST", "/api/chat"), ("GET", "/api/tags"),
    ]


def test_retag_during_inference_cannot_return_a_model_action(http):
    _, client = http
    model = OllamaModel("qwen3:4b-instruct")
    model.pin_model_digest("a" * 64)
    inventories = [Response(json.dumps({
        "models": [{"name": model.model_id, "digest": character * 64}],
    }).encode()) for character in ("a", "b")]
    chat(client, content='{"submit_preview":true}')
    client.getresponse.side_effect = [inventories[0], client.getresponse.return_value, inventories[1]]
    with pytest.raises(ModelError, match="digest changed"):
        turn(model)


def test_ollama_turn_round_trip(http):
    factory, client = http
    chat(client, calls=[
        {"name": "customer_search", "arguments": {"query": "Acme"}},
        {"name": "unlisted_tool", "arguments": {}},
    ])
    model = OllamaModel("qwen3:4b-instruct", "http://localhost:9412", context_window=16384)
    first = turn(model)
    assert first.final is None
    assert (first.input_tokens, first.output_tokens) == (50, 24)
    assert [(call.name, json.loads(call.arguments)) for call in first.calls] == [
        ("customer.search", {"query": "Acme"}),
        ("unlisted_tool", {}),
    ]
    assert len({call.id for call in first.calls}) == 2
    assert all(re.fullmatch(r"call_[0-9a-f]{32}", call.id) for call in first.calls)
    factory.assert_called_once()
    assert factory.call_args.args == ("127.0.0.1", 9412)
    assert 0 < factory.call_args.kwargs["timeout"] <= 3.0
    method, path, body, headers = client.request.call_args.args
    assert (method, path, headers["Content-Type"]) == ("POST", "/api/chat", "application/json")
    request = json.loads(body)
    assert request == {
        "model": "qwen3:4b-instruct",
        "messages": [
            {"role": "system", "content": "Select a tool or return a JSON object."},
            {"role": "user", "content": "Find Acme"},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "customer_search",
                "description": "customer.search",
                "parameters": declared("customer.search")["arguments"],
            },
        }],
        "stream": False,
        "think": False,
        "options": {"num_ctx": 16384, "num_predict": 80, "temperature": 0.0, "seed": 0},
    }

    transcript = request["messages"][1:]
    transcript.extend({
        "type": "function_call",
        "call_id": call.id,
        "name": call.name,
        "arguments": call.arguments,
    } for call in first.calls)
    transcript.extend({
        "type": "function_call_output",
        "call_id": call.id,
        "name": call.name,
        "output": '{"ok":true}' if call.name == "customer.search" else '{"ok":false}',
    } for call in first.calls)
    chat(client, content='{"answer":"Acme"}')
    assert turn(model, transcript=transcript) == ModelTurn(
        final='{"answer":"Acme"}', input_tokens=50, output_tokens=24
    )
    replay = json.loads(client.request.call_args.args[2])["messages"][2:]
    assert replay == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "function", "function": {
                    "index": 0, "name": "customer_search", "arguments": {"query": "Acme"},
                }},
                {"type": "function", "function": {
                    "index": 1, "name": "unlisted_tool", "arguments": {},
                }},
            ],
        },
        {"role": "tool", "tool_name": "customer_search", "content": '{"ok":true}'},
        {"role": "tool", "tool_name": "unlisted_tool", "content": '{"ok":false}'},
    ]


def test_replays_runner_error_for_malformed_arguments(http):
    _, client = http
    chat(client, calls=[{"name": "customer_search", "arguments": "{"}])
    call, = turn().calls
    assert call.arguments == "{"
    transcript = [
        {"role": "user", "content": "Find Acme"},
        {"type": "function_call", "call_id": call.id, "name": call.name, "arguments": call.arguments},
        {
            "type": "function_call_output",
            "call_id": call.id,
            "name": call.name,
            "output": '{"error":"Invalid tool arguments"}',
        },
    ]
    turn(transcript=transcript)
    assistant, result = json.loads(client.request.call_args.args[2])["messages"][2:]
    assert assistant["tool_calls"][0]["function"]["arguments"] == {}
    assert result["content"] == '{"error":"Invalid tool arguments"}'


@pytest.mark.parametrize("endpoint", [
    "http://example.com",
    "http://0.0.0.0:11434",
    "http://127.0.0.1.example.com",
    "http://user@127.0.0.1",
    "https://127.0.0.1",
    "http://127.0.0.1/file",
    "http://127.0.0.1:0",
])
def test_ollama_rejects_nonlocal_endpoints(endpoint):
    with pytest.raises(ModelError, match="loopback"):
        OllamaModel("model", endpoint)


def test_colliding_tool_names_fail_without_transport(http):
    factory, _ = http
    with pytest.raises(ModelError, match="collide"):
        turn(declarations=[declared("customer.search"), declared("customer_search")])
    factory.assert_not_called()


@pytest.mark.parametrize(("result", "error"), [
    (Response(b'{"error":"secret"}', status=307), "HTTP request failed"),
    (Response(b'{"error":"secret"}', status=500), "HTTP request failed"),
    (Response(b"secret"), "Invalid Ollama JSON response"),
    (Response(b'{"error":"secret"}'), "Invalid Ollama chat response"),
    (Response(b'{"done":false,"message":{"role":"assistant","content":"secret"}}'),
     "Invalid Ollama chat response"),
    (Response(b'{"done":true,"message":{"role":"assistant","tool_calls":[{}]}}'),
     "Invalid Ollama chat response"),
    (Response(b'{"done":true,"message":{"role":"assistant","content":"secret"},"eval_count":-1}'),
     "Invalid Ollama chat response"),
    (Response(b"x" * (2 * 1024 * 1024 + 1)), "response is too large"),
])
def test_ollama_rejects_errors_without_exposing_body(http, result, error):
    _, client = http
    client.getresponse.return_value = result
    with pytest.raises(ModelError, match=error) as failure:
        turn()
    assert "secret" not in str(failure.value)
    client.close.assert_called_once()


def test_ollama_sanitizes_timeouts_and_bounds_request_size(http):
    factory, client = http
    with pytest.raises(ModelError, match="request is too large"):
        turn(instructions="x" * (2 * 1024 * 1024))
    factory.assert_not_called()
    client.getresponse.side_effect = TimeoutError("secret")
    with pytest.raises(ModelError, match="request timed out") as failure:
        turn()
    assert "secret" not in str(failure.value)
    client.close.assert_called_once()


def test_ollama_model_digest(http):
    _, client = http
    digest = "0123456789abcdef" * 4
    client.getresponse.return_value = Response(json.dumps({"models": [
        {"name": "other:latest", "digest": "a" * 64},
        {"model": "qwen3:latest", "digest": digest},
        {"name": "gemma4", "digest": digest},
    ]}).encode())
    assert OllamaModel("qwen3").model_digest() == digest
    assert OllamaModel("gemma4:latest").model_digest() == digest
    assert client.request.call_args.args[:3] == ("GET", "/api/tags", None)
    with pytest.raises(ModelError, match="digest unavailable"):
        OllamaModel("different").model_digest()
