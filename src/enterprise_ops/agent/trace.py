"""Local artifacts for a single synthetic run."""

import json
import re
from contextlib import closing
from pathlib import Path
from typing import Any

from enterprise_ops.sandbox import RunSandbox
from enterprise_ops.storage.audit import stamp
from enterprise_ops.storage.sqlite import connect


_PRIVATE_KEYS = {
    "api_key", "authorization", "password", "secret", "access_token",
    "preview_token", "idempotency_key", "command_key", "cursor", "next_cursor",
}
_CREDENTIAL = re.compile(
    r"(?i)(?:\b(?:Bearer\s+[A-Za-z0-9._~+/-]{8,}|sk-[A-Za-z0-9_-]{8,})|"
    r"(?:preview|command)_[0-9a-f]{32}|[A-Za-z0-9_+/=-]{96,})"
)


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            scrub(key): "[redacted]" if key.casefold() in _PRIVATE_KEYS and item is not None else scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return _CREDENTIAL.sub("[redacted]", value)
    return value


class RunTrace:
    def __init__(self, root: Path, sandbox: RunSandbox):
        self.path = root / f"{sandbox.run_id}-{sandbox.id}"
        self.path.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.as_of = stamp(sandbox.clock.instant)
        self.index = 0

    def write(self, name: str, value: Any) -> None:
        target = self.path / f"{name}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(scrub(value), indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def event(self, kind: str, **fields: Any) -> None:
        self.index += 1
        value = {"ordinal": self.index, "event": kind, "as_of": self.as_of, **fields}
        with (self.path / "trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(scrub(value)) + "\n")

    def state_delta(self, sandbox: RunSandbox, tenant_id: str) -> None:
        with closing(connect(sandbox.path)) as db:
            rows = db.execute(
                "SELECT id, call_id, actor_id, action, subject_id, reason_code, before_json,"
                " after_json, evidence_json, state_revision FROM audit_events"
                " WHERE tenant_id = ? AND run_id = ? ORDER BY state_revision, rowid",
                (tenant_id, sandbox.run_id),
            )
            delta = []
            for row in rows:
                entry = dict(row)
                for key in ("before", "after", "evidence"):
                    data = entry.pop(f"{key}_json")
                    entry[key] = json.loads(data) if data is not None else None
                delta.append(entry)
        self.write("state_delta", delta)
