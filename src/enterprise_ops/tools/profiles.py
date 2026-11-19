"""Snapshot-selected public argument contracts for the shared tool handlers."""

from dataclasses import dataclass, field

from pydantic import Field

from enterprise_ops.tools.contracts import Cursor, Id, PageSize, SchemaVersion, StrictModel
from enterprise_ops.tools.reads import InvoiceSearchArgs


class InvoiceSearchV2Args(InvoiceSearchArgs):
    # Validation aliases expose the v2 names while retaining canonical fields for the handler.
    customer_id: Id = Field(validation_alias="account_ref")
    limit: PageSize = Field(default=10, validation_alias="page_size")
    cursor: Cursor | None = Field(default=None, validation_alias="page_cursor")


@dataclass(frozen=True)
class ToolProfile:
    schema_version: SchemaVersion
    argument_overrides: dict[str, type[StrictModel]] = field(default_factory=dict)


TOOL_PROFILE = "enterprise_ops:v1"
TOOL_PROFILES = {
    TOOL_PROFILE: ToolProfile("v1"),
    "enterprise_ops:v2": ToolProfile("v2", {"invoice.search": InvoiceSearchV2Args}),
}
