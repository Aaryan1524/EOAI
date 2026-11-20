"""Run GUARD model roles through Seatbelt and a narrow loopback inference relay."""

import json
import subprocess
from collections.abc import Callable
from hashlib import sha256
from itertools import count
from pathlib import Path
from time import monotonic

from enterprise_ops.agent.model import OllamaModel, strict_json

from experiments.eoai_eval.isolation import _invoke, role_area


_PURPOSES = {
    ("reviewer", "checklist"): (
        {"request", "public_context", "tool_declarations", "deterministic_criteria", "semantic_options"}, 512,
    ),
    ("reviewer", "criterion"): (
        {"request", "public_context", "tool_names", "criterion", "evidence"}, 512,
    ),
    ("candidate-generator", "distill"): ({"strategy", "incumbent", "regret", "tool_declarations"}, 2048),
    ("distiller", "repair"): ({"incumbent", "candidate", "gate", "regret", "tool_declarations"}, 2048),
    ("distiller", "contrast"): ({"best", "worst"}, 2048),
}
_SEED_COUNT = 2**31
_MAX_INSTRUCTION_BYTES = 16 * 1024
_MAX_PAYLOAD_BYTES = 128 * 1024
_EDIT_FIELDS = {
    "condition": {"type": "string"},
    "rule": {"type": "string"},
    "why": {"type": "string"},
}


def _role_format(purpose: str, payload: dict) -> dict:
    if purpose == "checklist":
        fields = {"selected": {"type": "array",
                               "items": {"type": "string", "enum": list(payload["semantic_options"])}}}
    elif purpose in ("distill", "repair"):
        fields = dict(_EDIT_FIELDS)
        if purpose == "distill":
            fields["field"] = {"type": "string", "enum": ["instructions", "skills", "memory"]}
    elif purpose == "contrast":
        fields = {
            "learnings": {"type": "array", "items": {"type": "string"}},
            "updated_best_prompt": {"type": "string"}, "updated_worst_prompt": {"type": "string"},
        }
    else:
        fields = {
            "verdict": {"type": "string", "enum": ["PASS", "FAIL", "UNKNOWN"]},
            "why": {"type": "string"},
            "evidence": {"type": "array", "items": {
                "type": "object", "required": ["key", "quote", "finding"], "additionalProperties": False,
                "properties": {
                    "key": {"type": "string"},
                    "quote": {"type": "string"},
                    "finding": {"type": "string", "enum": ["SUPPORTS", "CONTRADICTS"]},
                },
            }},
        }
    return {"type": "object", "required": sorted(fields), "additionalProperties": False, "properties": fields}


_WORKER = """
import json
import sys

def unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Duplicate field")
    return fields

def encode(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)

try:
    data = json.load(sys.stdin, object_pairs_hook=unique_fields)
    if data["operation"] == "prepare":
        result = [
            {"role": "system", "content": data["instruction"]},
            {"role": "user", "content": encode(data["payload"])},
        ]
    elif data["operation"] == "validate":
        message = data["message"]
        if (
            data["done"] is not True
            or data["done_reason"] not in (None, "stop")
            or not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("tool_calls", []) != []
            or not isinstance(message.get("content"), str)
        ):
            raise ValueError("Invalid assistant response")
        result = json.loads(message["content"], object_pairs_hook=unique_fields)
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
    else:
        raise ValueError("Unknown operation")
    output = encode({"result": result})
except (KeyError, TypeError, ValueError, RecursionError):
    output = '{"error":"invalid_json"}'
print(output)
"""


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)


class RoleModel:
    """Call a pinned Ollama model with public JSON supplied by the lesson runner.

    The caller constructs the public view for each purpose and verifies the native
    role boundary before a study. The relay keeps network access and configuration
    in the trusted process. The worker sees just its prompt or assistant reply.
    """

    def __init__(
        self,
        model_digest: str,
        *,
        model_id: str = "qwen3:4b-instruct",
        endpoint: str = "http://127.0.0.1:11434",
        seed: int = 0,
        timeout: float = 90.0,
        context_window: int = 32768,
    ) -> None:
        if type(seed) is not int or not 0 <= seed < _SEED_COUNT:
            raise ValueError("Invalid role sampling seed")
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError("Invalid role timeout")
        if type(context_window) is not int or not 2048 <= context_window <= 32768:
            raise ValueError("Invalid role context window")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("Invalid role model ID")
        self._model = OllamaModel(model_id, endpoint, context_window=context_window)
        self._digest = self._model.pin_model_digest(model_digest)
        self._seed = seed
        self._samples = count()
        self._timeout = timeout
        self._context_window = context_window
        self._metrics: list[dict] = []

    @property
    def model_digest(self) -> str:
        return self._digest

    @property
    def metrics(self) -> tuple[dict, ...]:
        return tuple(row.copy() for row in self._metrics)

    def _sandbox(self, role: str, area: Path, data: dict, deadline: float) -> object:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError("Role call deadline expired")
        try:
            output = _invoke(role, area, _WORKER, data, timeout=min(remaining, 15)).stdout
            envelope = strict_json(output)
        except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
            raise RuntimeError("Role sandbox invocation failed") from None
        if envelope == {"error": "invalid_json"}:
            raise ValueError("Invalid JSON in role worker")
        if not isinstance(envelope, dict) or envelope.keys() != {"result"}:
            raise RuntimeError("Invalid role sandbox envelope")
        return envelope["result"]

    def _check_digest(self, deadline: float) -> None:
        if self._model.model_digest(timeout=deadline - monotonic()) != self.model_digest:
            raise RuntimeError("Installed role model digest changed")

    def __call__(
        self, role: str, purpose: str, instruction: str, payload: dict, temperature: float,
        *, record_raw: Callable[[str | None], None] | None = None,
    ) -> dict:
        start = monotonic()
        deadline = start + self._timeout
        operation = (role, purpose) if type(role) is str and type(purpose) is str else None
        spec = _PURPOSES.get(operation)
        metrics = {
            "role": role if spec else None,
            "purpose": purpose if spec else None,
            "temperature": None,
            "seed": None,
            "input_tokens": None,
            "output_tokens": None,
            "status": "invalid_request",
        }
        try:
            if spec is None:
                raise ValueError("Unknown role purpose")
            fields, max_output_tokens = spec
            if (
                type(instruction) is not str
                or not instruction.strip()
                or type(payload) is not dict
                or payload.keys() != fields
                or type(temperature) not in (int, float)
                or not 0 <= temperature <= 0.9
            ):
                raise ValueError("Invalid role input")
            try:
                user = _json(payload)
                if (
                    len(instruction.encode()) > _MAX_INSTRUCTION_BYTES
                    or len(user.encode()) > _MAX_PAYLOAD_BYTES
                ):
                    raise ValueError
            except (TypeError, ValueError, RecursionError):
                raise ValueError("Invalid or oversized role prompt") from None
            seed = (self._seed + next(self._samples)) % _SEED_COUNT if temperature else self._seed
            metrics.update(temperature=temperature, seed=seed, status="sandbox_failed")
            with role_area() as area:
                messages = self._sandbox(role, area, {
                    "operation": "prepare", "instruction": instruction, "payload": payload,
                }, deadline)
                expected = [{"role": "system", "content": instruction}, {"role": "user", "content": user}]
                if messages != expected:
                    raise RuntimeError("Unexpected role sandbox prompt")

                body = _json({
                    "model": self._model.model_id,
                    "messages": messages,
                    "format": _role_format(purpose, payload),
                    "stream": False,
                    "think": False,
                    "options": {
                        "num_ctx": self._context_window,
                        "num_predict": max_output_tokens,
                        "temperature": temperature,
                        "seed": seed,
                    },
                }).encode()
                metrics["prompt_sha256"] = sha256(_json(messages).encode()).hexdigest()
                metrics["status"] = "model_identity_failed"
                self._check_digest(deadline)
                metrics["status"] = "provider_failed"
                response = self._model._request("POST", "/api/chat", deadline - monotonic(), body)
                message = response.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                raw_reply = content if isinstance(content, str) else None
                if role == "reviewer":
                    metrics["raw_reply"] = raw_reply
                if record_raw is not None:
                    record_raw(raw_reply)
                for source, target in (("prompt_eval_count", "input_tokens"), ("eval_count", "output_tokens")):
                    tokens = response.get(source)
                    if type(tokens) is int and tokens >= 0:
                        metrics[target] = tokens
                metrics["status"] = "model_identity_failed"
                self._check_digest(deadline)

                assistant = (
                    {key: message[key] for key in ("role", "content", "tool_calls") if key in message}
                    if isinstance(message, dict) else None
                )
                metrics["status"] = "invalid_response"
                try:
                    result = self._sandbox(role, area, {
                        "operation": "validate", "message": assistant,
                        "done": response.get("done"), "done_reason": response.get("done_reason"),
                    }, deadline)
                except ValueError as error:
                    if role == "reviewer":
                        metrics["response_parse_error"] = str(error)
                    raise
                if not isinstance(result, dict):
                    raise ValueError("Expected role JSON object")
                metrics["response_sha256"] = sha256(_json(result).encode()).hexdigest()
            metrics["status"] = "ok"
            return result
        except OSError:
            raise RuntimeError("Role sandbox workspace failed") from None
        finally:
            metrics["latency_seconds"] = monotonic() - start
            self._metrics.append(metrics)
