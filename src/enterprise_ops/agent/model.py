"""Provider-neutral model turns and the local Ollama adapter."""

import http.client
import json
import math
import re
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from threading import Timer
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelTurn:
    calls: tuple[ToolCall, ...] = ()
    final: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class Model(Protocol):
    model_id: str
    provider: str

    def turn(
        self,
        *,
        instructions: str,
        transcript: Sequence[dict],
        declarations: Sequence[dict],
        timeout: float,
        max_output_tokens: int,
    ) -> ModelTurn: ...


class ModelError(RuntimeError):
    """Model boundary failure with a safe, static message."""


_MAX_BODY_BYTES = 2 * 1024 * 1024


def _local_address(endpoint: str) -> tuple[str, int]:
    try:
        url = urlsplit(endpoint)
        hostname = url.hostname
        if (
            url.scheme != "http"
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or not hostname
        ):
            raise ValueError
        if hostname == "localhost":
            hostname = "127.0.0.1"
        address = ip_address(hostname)
        if not address.is_loopback or "%" in hostname or url.port == 0:
            raise ValueError
        return str(address), url.port or 80
    except ValueError:
        raise ModelError("Ollama endpoint must be a loopback HTTP address") from None


def _stop_request(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # The server or caller may have closed the socket first.


def _provider_tools(declarations: Sequence[dict]) -> tuple[list[dict], dict[str, str]]:
    tools = []
    logical_names: dict[str, str] = {}
    for declaration in declarations:
        logical = declaration["name"]
        parameters = declaration["arguments"]
        name = logical.replace(".", "_")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not isinstance(parameters, dict):
            raise ModelError("Invalid model tool declaration")
        if name in logical_names:
            raise ModelError("Model tool names collide")
        logical_names[name] = logical
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": declaration.get("description", logical),
                "parameters": parameters,
            },
        })
    return tools, logical_names


def _provider_messages(
    instructions: str, transcript: Sequence[dict], logical_names: dict[str, str]
) -> list[dict]:
    aliases = {logical: provider for provider, logical in logical_names.items()}
    messages: list[dict] = [{"role": "system", "content": instructions}]
    for entry in transcript:
        kind = entry.get("type")
        if kind == "function_call":
            if messages[-1]["role"] != "assistant":
                messages.append({"role": "assistant", "content": "", "tool_calls": []})
            calls = messages[-1]["tool_calls"]
            try:
                arguments = json.loads(entry["arguments"])
            except ValueError:
                arguments = None
            calls.append({
                "type": "function",
                "function": {
                    "index": len(calls),
                    "name": aliases.get(entry["name"], entry["name"]),
                    # Ollama requires an object even when the runner rejected the arguments.
                    "arguments": arguments if isinstance(arguments, dict) else {},
                },
            })
        elif kind == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_name": aliases.get(entry["name"], entry["name"]),
                "content": entry["output"],
            })
        elif entry.get("role") == "user":
            messages.append({"role": "user", "content": entry["content"]})
        else:
            raise ModelError("Invalid model transcript entry")
    return messages


def _model_turn(response: dict, logical_names: dict[str, str]) -> ModelTurn:
    message = response["message"]
    if response.get("done") is not True or message.get("role") != "assistant":
        raise ValueError
    calls = message.get("tool_calls", [])
    content = message.get("content", "")
    if not isinstance(calls, list) or not isinstance(content, str):
        raise ValueError
    if not calls and "content" not in message:
        raise ValueError

    translated = []
    for call in calls:
        function = call["function"]
        name = function["name"]
        arguments = function["arguments"]
        if not isinstance(name, str) or not name or not isinstance(arguments, (dict, str)):
            raise ValueError
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, separators=(",", ":"), allow_nan=False)
        # IDs belong to the runner transcript and are never sent back to Ollama.
        translated.append(ToolCall(f"call_{uuid4().hex}", logical_names.get(name, name), arguments))

    input_tokens = response.get("prompt_eval_count", 0)
    output_tokens = response.get("eval_count", 0)
    if any(type(count) is not int or count < 0 for count in (input_tokens, output_tokens)):
        raise ValueError
    return ModelTurn(
        calls=tuple(translated),
        final=None if translated else content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class OllamaModel:
    """Translate chat requests to a trusted, loopback Ollama daemon."""

    provider = "ollama"

    def __init__(
        self,
        model_id: str,
        endpoint: str = "http://127.0.0.1:11434",
        *,
        context_window: int = 32768,
    ) -> None:
        self.model_id = model_id
        self._host, self._port = _local_address(endpoint)
        if type(context_window) is not int or context_window <= 0:
            raise ModelError("Invalid model context window")
        self._context_window = context_window

    def turn(
        self,
        *,
        instructions: str,
        transcript: Sequence[dict],
        declarations: Sequence[dict],
        timeout: float,
        max_output_tokens: int,
    ) -> ModelTurn:
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ModelError("Invalid model output token limit")
        try:
            tools, logical_names = _provider_tools(declarations)
            messages = _provider_messages(instructions, transcript, logical_names)
            payload = {
                "model": self.model_id,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "think": False,
                "options": {
                    "num_ctx": self._context_window,
                    "num_predict": max_output_tokens,
                    "temperature": 0.0,
                    "seed": 0,
                },
            }
            body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
            raise ModelError("Invalid model request") from None

        response = self._request("POST", "/api/chat", timeout, body)
        try:
            return _model_turn(response, logical_names)
        except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
            raise ModelError("Invalid Ollama chat response") from None

    def model_digest(self, *, timeout: float = 5.0) -> str:
        response = self._request("GET", "/api/tags", timeout)
        model_ids = {self.model_id}
        if self.model_id.endswith(":latest"):
            model_ids.add(self.model_id.removesuffix(":latest"))
        elif ":" not in self.model_id.rsplit("/", 1)[-1]:
            model_ids.add(f"{self.model_id}:latest")
        try:
            models = response["models"]
            if not isinstance(models, list):
                raise TypeError
            for model in models:
                if any(model.get(key) in model_ids for key in ("model", "name")):
                    digest = model["digest"]
                    if re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", digest):
                        return digest
        except (AttributeError, KeyError, TypeError):
            raise ModelError("Invalid Ollama model inventory") from None
        raise ModelError("Ollama model digest unavailable")

    def _request(
        self, method: str, path: str, timeout: float, body: bytes | None = None
    ) -> dict:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ModelError("Invalid model timeout")
        if body is not None and len(body) > _MAX_BODY_BYTES:
            raise ModelError("Model request is too large")

        deadline = monotonic() + timeout
        connection = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        timer = None
        try:
            connection.connect()
            remaining = deadline - monotonic()
            sock = connection.sock
            if remaining <= 0 or sock is None:
                raise TimeoutError
            # Socket timeouts limit inactivity. Close the socket at the deadline
            # even when a peer continues sending fragments of the HTTP response.
            sock.settimeout(remaining)
            timer = Timer(remaining, _stop_request, args=(sock,))
            timer.daemon = True
            timer.start()
            headers = {"Accept": "application/json", "Connection": "close"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body, headers)
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ModelError("Ollama HTTP request failed")
                raw = response.read(_MAX_BODY_BYTES + 1)
            if monotonic() >= deadline:
                raise TimeoutError
        except TimeoutError:
            raise ModelError("Ollama request timed out") from None
        except (OSError, http.client.HTTPException, OverflowError):
            if monotonic() >= deadline:
                raise ModelError("Ollama request timed out") from None
            raise ModelError("Ollama HTTP request failed") from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

        if len(raw) > _MAX_BODY_BYTES:
            raise ModelError("Ollama response is too large")
        try:
            response_data = json.loads(raw)
        except (ValueError, RecursionError):
            raise ModelError("Invalid Ollama JSON response") from None
        if not isinstance(response_data, dict):
            raise ModelError("Invalid Ollama JSON response")
        return response_data
