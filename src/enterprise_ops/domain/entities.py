"""Shared identities and policy inputs."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Capability(StrEnum):
    READ = "read"
    CREATE_TICKET = "create_ticket"
    REQUEST_APPROVAL = "request_approval"
    DECIDE_APPROVAL = "decide_approval"
    SIMULATE_SOURCE_CHANGE = "simulate_source_change"


ROLE_CAPABILITIES = {
    "viewer": frozenset({Capability.READ}),
    "analyst": frozenset({Capability.READ, Capability.CREATE_TICKET, Capability.REQUEST_APPROVAL}),
    "supervisor": frozenset({Capability.READ, Capability.DECIDE_APPROVAL}),
    "simulator": frozenset({Capability.READ, Capability.SIMULATE_SOURCE_CHANGE}),
}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str


@dataclass(frozen=True)
class Invoice:
    id: str
    customer_id: str
    contract_id: str | None
    issued_at: datetime | None
    currency: str
    total_minor: int
    status: str
    version: int


@dataclass(frozen=True)
class Dispute:
    id: str
    opened_at: datetime
    closed_at: datetime | None

    def is_active(self, instant: datetime) -> bool:
        return self.opened_at <= instant and (self.closed_at is None or instant < self.closed_at)


@dataclass(frozen=True)
class EscalationPolicy:
    id: str
    version: int
    threshold_days: int
    exclude_disputed: bool
    requires_approval: bool
    excluded_customer_statuses: frozenset[str] = frozenset()
    excluded_invoice_statuses: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EscalationRestrictions:
    threshold_days: int | None = None
    exclude_disputed: bool = False

    def __post_init__(self) -> None:
        if self.threshold_days is not None and (
            type(self.threshold_days) is not int or self.threshold_days < 0
        ):
            raise ValueError("The request threshold must be a nonnegative integer")
