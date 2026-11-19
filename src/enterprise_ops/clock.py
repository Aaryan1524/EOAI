"""A single simulation instant with tenant-local calendar semantics."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class FrozenClock:
    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.utcoffset() is None:
            raise ValueError("The simulation instant must include a timezone")
        object.__setattr__(self, "instant", self.instant.astimezone(UTC))

    def business_date(self, timezone: str) -> date:
        return self.instant.astimezone(ZoneInfo(timezone)).date()
