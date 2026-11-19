"""Allowed simulator transitions for mutable source records."""


class InvalidTransition(ValueError):
    """The current state cannot transition to the requested state."""


INVOICE_TRANSITIONS = {
    "draft": frozenset({"issued", "cancelled"}),
    "issued": frozenset({"void", "cancelled"}),
}

PAYMENT_TRANSITIONS = {
    "pending": frozenset({"posted", "failed"}),
}


def require_transition(
    allowed: dict[str, frozenset[str]], current: str, target: str
) -> None:
    if target not in allowed.get(current, ()):
        raise InvalidTransition(f"Cannot change {current} to {target}")
