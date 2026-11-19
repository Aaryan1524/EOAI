"""Short references for opaque values presented to the model."""

from collections import defaultdict
from typing import Any


_KINDS = {
    "cursor": "cursor", "next_cursor": "cursor", "page_cursor": "cursor",
    "preview_token": "preview", "idempotency_key": "command",
    "evidence_id": "evidence", "evidence_ids": "evidence",
}


class ToolHandles:
    def __init__(self) -> None:
        self._handles: dict[str, dict[str, str]] = defaultdict(dict)
        self._values: dict[str, dict[str, str]] = defaultdict(dict)

    def _encode(self, kind: str, value: Any) -> Any:
        if isinstance(value, list):
            return [self._encode(kind, item) for item in value]
        if not isinstance(value, str):
            return value
        handles = self._handles[kind]
        if value not in handles:
            handle = f"{kind}_{len(handles) + 1}"
            handles[value] = handle
            self._values[kind][handle] = value
        return handles[value]

    def for_model(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._encode(_KINDS[key], item) if key in _KINDS else self.for_model(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.for_model(item) for item in value]
        return value

    def for_gateway(self, arguments: dict | None) -> dict | None:
        if arguments is None:
            return None
        resolved = arguments.copy()
        for key, kind in _KINDS.items():
            value = resolved.get(key)
            known = self._values[kind]
            if isinstance(value, str):
                resolved[key] = known.get(value, value)
            elif key == "evidence_ids" and isinstance(value, list):
                resolved[key] = [known.get(item, item) if isinstance(item, str) else item for item in value]
        return resolved
