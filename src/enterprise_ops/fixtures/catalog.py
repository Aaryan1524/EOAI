"""Built-in synthetic task assignments for local sandbox runs."""

from dataclasses import dataclass, field, replace

from enterprise_ops.agent.runner import RunTask
from enterprise_ops.domain.entities import Capability, Principal
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep
from enterprise_ops.tools.session import WriteScope


@dataclass(frozen=True)
class CatalogTask:
    task: RunTask
    scenario: str
    profile: str
    level: int
    faults: FaultPlan = field(default_factory=FaultPlan)


_NORTHSTAR = Principal("northstar_property", "analyst")
_HARBORLINE = Principal("harborline_operations", "analyst")
_V1 = "enterprise_ops:v1"
_V2 = "enterprise_ops:v2"

_CATALOG = {
    "acme-list": CatalogTask(
        RunTask(
            "acme-list", "List every invoice for Acme Property Group.",
            _NORTHSTAR, "list_invoices", customer_query="Acme Property Group",
        ),
        scenario="baseline", profile=_V1, level=1,
    ),
    "acme-escalation": CatalogTask(
        RunTask(
            "acme-escalation",
            "Review Acme Property Group invoice INV-104 under current policy. "
            "If eligible, create and verify an escalation ticket when authorized.",
            _NORTHSTAR, "escalate", customer_id="acme", invoice_ids=frozenset({"INV-104"}),
        ),
        scenario="baseline", profile=_V1, level=2,
    ),
    "meridian-conflict": CatalogTask(
        RunTask(
            "meridian-conflict",
            "Review the payment terms and escalation eligibility for Meridian Property Services "
            "invoice INV-M01. Cite the contractual sources and flag any question that needs review.",
            _NORTHSTAR, "review_invoices", customer_id="meridian", invoice_ids=frozenset({"INV-M01"}),
        ),
        scenario="baseline", profile=_V1, level=2,
    ),
    "acme-paged-v2": CatalogTask(
        RunTask(
            "acme-paged-v2",
            "Verify the customer search for Acme Property Group and use the exact legal name to "
            "identify the account. Review every invoice using pages of three and follow all continuation "
            "cursors. Compare the recorded and contractual due dates and cite the controlling terms.",
            _NORTHSTAR, "review_invoices", customer_query="Acme Property Group",
        ),
        scenario="acme-paged-v2", profile=_V2, level=3,
    ),
    "compound-escalation": CatalogTask(
        RunTask(
            "compound-escalation",
            "Review every invoice for Harborline Facilities LLC in pages of three. Evaluate contractual "
            "payment terms, posted payments, disputes, and current escalation policy. Check for existing "
            "tickets, create and verify required tickets when authorized, and report items requiring review.",
            _HARBORLINE, "escalate", customer_id="harborline",
        ),
        scenario="compound-escalation", profile=_V2, level=4,
        faults=FaultPlan((FaultStep("invoice.search", 1, FaultKind.TRANSIENT_FAILURE),)),
    ),
}


def task_names() -> tuple[str, ...]:
    """List public task names in order of complexity."""
    return tuple(_CATALOG)


def load_task(name: str, *, authorize_writes: bool = False) -> CatalogTask:
    """Return a fresh assignment, with write scope only when granted by the host."""
    if type(authorize_writes) is not bool:
        raise ValueError("authorize_writes must be a boolean")
    try:
        entry = _CATALOG[name]
    except KeyError as error:
        raise ValueError(f"Unknown built-in task: {name!r}") from error

    task = entry.task
    if authorize_writes and task.operation == "escalate" and task.customer_id is not None:
        scope = WriteScope(
            principal=task.principal, scope_id=f"catalog-{task.id}", customer_id=task.customer_id,
            capabilities=frozenset({Capability.CREATE_TICKET}), invoice_ids=task.invoice_ids,
        )
        task = replace(task, write_scope=scope)
    return replace(entry, task=task, faults=entry.faults.fresh())
