"""Trusted run scope and observation state behind the gateway."""

import base64
import binascii
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from threading import Lock
from time import monotonic

from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import Capability, EscalationRestrictions, Principal
from enterprise_ops.domain.policy import Assessment
from enterprise_ops.sandbox import RunSandbox
from enterprise_ops.storage.audit import canonical
from enterprise_ops.tools.contracts import PageArgs, ToolFailure


@dataclass(frozen=True)
class WriteScope:
    """An operator's authorization for this customer and these simulated actions."""

    principal: Principal
    scope_id: str
    customer_id: str
    capabilities: frozenset[Capability]
    invoice_ids: frozenset[str] | None = None
    restrictions: EscalationRestrictions = EscalationRestrictions()

    def __post_init__(self) -> None:
        capabilities = frozenset(self.capabilities)
        if not capabilities <= {Capability.CREATE_TICKET, Capability.REQUEST_APPROVAL}:
            raise ValueError("The scope can grant only ticket and approval-request capabilities")
        object.__setattr__(self, "capabilities", capabilities)
        if self.invoice_ids is not None:
            object.__setattr__(self, "invoice_ids", frozenset(self.invoice_ids))

    @property
    def digest(self) -> str:
        return digest({
            "principal": asdict(self.principal),
            "scope_id": self.scope_id,
            "customer_id": self.customer_id,
            "capabilities": sorted(self.capabilities),
            "invoice_ids": None if self.invoice_ids is None else sorted(self.invoice_ids),
            "threshold_days": self.restrictions.threshold_days,
            "exclude_disputed": self.restrictions.exclude_disputed,
        })


@dataclass(frozen=True)
class Observation:
    id: str
    source_type: str
    source_id: str
    source_version: int
    locator: str
    invoice_id: str | None
    revision: int
    call_id: str


@dataclass(frozen=True)
class Preview:
    action: str
    key: str
    invoice_id: str
    scope_digest: str
    reason_code: str
    evidence_ids: tuple[str, ...]
    approval_id: str | None
    revision: int
    invoice_version: int
    policy_version: int
    expires_at: float


@dataclass
class Session:
    sandbox: RunSandbox
    principal: Principal
    write_scope: WriteScope | None
    elapsed: Callable[[], float] = monotonic
    signing_key: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    observations: dict[str, Observation] = field(default_factory=dict)
    previews: dict[str, Preview] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock, repr=False)

    def offer(self, token: str, preview: Preview) -> None:
        with self.lock:
            now = self.elapsed()
            self.previews = {key: value for key, value in self.previews.items() if value.expires_at > now}
            self.previews[token] = preview

    def preview(self, token: str) -> Preview | None:
        with self.lock:
            return self.previews.get(token)

    def observe(self, observations: dict[str, Observation]) -> None:
        with self.lock:
            self.observations.update(observations)

    def observed(self, identifier: str) -> Observation | None:
        with self.lock:
            return self.observations.get(identifier)


@dataclass
class ToolCall:
    connection: sqlite3.Connection
    session: Session
    name: str
    call_id: str
    timezone: str
    revision: int
    observations: dict[str, Observation] = field(default_factory=dict)

    @property
    def principal(self) -> Principal:
        return self.session.principal

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    @property
    def clock(self) -> FrozenClock:
        return self.session.sandbox.clock

    @property
    def restrictions(self) -> EscalationRestrictions:
        scope = self.session.write_scope
        return scope.restrictions if scope else EscalationRestrictions()

    def evidence(
        self,
        source_type: str,
        source_id: str,
        source_version: int,
        locator: str,
        *,
        invoice_id: str | None = None,
    ) -> str:
        immutable = source_type in {"document_span", "audit_event"}
        identifier = "ev_" + digest([
            self.session.sandbox.id, self.tenant_id, source_type, source_id, source_version,
            locator, None if immutable else self.revision,
        ])
        self.observations[identifier] = Observation(
            identifier, source_type, source_id, source_version, locator, invoice_id,
            self.revision, self.call_id,
        )
        return identifier

    def _cursor_context(self, args: PageArgs) -> list[str]:
        return [
            self.session.sandbox.id, self.tenant_id, self.session.sandbox.profile, self.name,
            digest(args.model_dump(exclude={"cursor"})),
        ]

    def cursor(self, args: PageArgs, last_id: str, *, empty: bool = False) -> str:
        marker = self.call_id if empty else None
        body = canonical([self._cursor_context(args), self.revision, last_id, marker]).encode()
        signature = hmac.digest(self.session.signing_key, body, "sha256")
        return base64.urlsafe_b64encode(signature + body).decode()

    def seek(self, args: PageArgs) -> str:
        if args.cursor is None:
            return ""
        try:
            raw = base64.b64decode(args.cursor, altchars=b"-_", validate=True)
            signature, body = raw[:32], raw[32:]
            expected = hmac.digest(self.session.signing_key, body, "sha256")
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            context, revision, last_id, _ = json.loads(body)
            if context != self._cursor_context(args):
                raise ValueError
        except (ValueError, binascii.Error, UnicodeError, TypeError) as error:
            raise ToolFailure("VALIDATION_ERROR", "Invalid pagination cursor") from error
        if revision != self.revision:
            raise ToolFailure("STALE_REVISION", "Pagination cursor refers to an older tenant revision")
        return last_id


def digest(value: object) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def assessment_locator(assessment: Assessment) -> str:
    return digest(asdict(assessment))
