"""Atomic revision and audit helpers for trusted write transactions."""

import json
import sqlite3
from datetime import datetime
from uuid import uuid4

from enterprise_ops.domain.entities import Principal


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stamp(instant: datetime) -> str:
    return instant.isoformat(timespec="microseconds")


def bump_revision(connection: sqlite3.Connection, tenant_id: str) -> int:
    return connection.execute(
        "UPDATE tenants SET revision = revision + 1 WHERE id = ? RETURNING revision", (tenant_id,)
    ).fetchone()[0]


def append_event(
    connection: sqlite3.Connection,
    principal: Principal,
    action: str,
    subject_id: str,
    instant: datetime,
    revision: int,
    *,
    reason_code: str,
    before: dict | None,
    after: dict,
    evidence_ids: tuple[str, ...] = (),
    run_id: str = "simulator",
    call_id: str | None = None,
) -> str:
    identifier = f"audit_{uuid4().hex}"
    connection.execute(
        "INSERT INTO audit_events "
        "(tenant_id, id, run_id, call_id, actor_id, action, subject_id, reason_code, "
        "before_json, after_json, evidence_json, observed_at, state_revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            principal.tenant_id, identifier, run_id, call_id or f"call_{uuid4().hex}",
            principal.user_id, action, subject_id, reason_code,
            canonical(before) if before is not None else None, canonical(after),
            canonical(evidence_ids), stamp(instant), revision,
        ),
    )
    return identifier
