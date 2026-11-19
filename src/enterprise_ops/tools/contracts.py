"""Shared schemas for tool declarations and responses."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise_ops.domain.entities import Capability


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


Id = Annotated[str, Field(min_length=1, max_length=128)]
Cursor = Annotated[str, Field(min_length=1, max_length=2048)]
PageSize = Annotated[int, Field(ge=1, le=25)]
Revision = Annotated[int, Field(ge=0)]
Version = Annotated[int, Field(ge=1)]
SchemaVersion = Literal["v1", "v2"]
ErrorCode = Literal[
    "NOT_FOUND", "PERMISSION_DENIED", "VALIDATION_ERROR", "SCHEMA_MISMATCH",
    "RATE_LIMITED", "TRANSIENT_FAILURE", "CONFLICT", "PRECONDITION_FAILED",
    "STALE_REVISION", "AMBIGUOUS_RESULT", "UNKNOWN_ERROR",
]


class PageArgs(StrictModel):
    limit: PageSize = 10
    cursor: Cursor | None = None


Item = TypeVar("Item", bound=StrictModel)


class Page(StrictModel, Generic[Item]):
    items: list[Item]
    next_cursor: Cursor | None


class ToolError(StrictModel):
    code: ErrorCode
    message: str
    retryable: bool
    retry_after_ms: int | None = None


class ToolFailure(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error = ToolError(
            code=code, message=message, retryable=retryable, retry_after_ms=retry_after_ms
        )


class ToolMeta(StrictModel):
    tool_name: str
    schema_version: SchemaVersion = "v1"
    profile: str
    sandbox_id: Id
    state_revision: Revision | None
    observed_at: str
    call_id: Id
    evidence_ids: list[Id]


class ToolResponse(StrictModel, Generic[Item]):
    ok: bool
    data: Item | None
    error: ToolError | None
    meta: ToolMeta

    @model_validator(mode="after")
    def check_outcome(self) -> "ToolResponse[Item]":
        if self.ok != (self.data is not None) or self.ok == (self.error is not None):
            raise ValueError("Tool result must have exactly one success or error payload")
        return self


@dataclass(frozen=True)
class ToolDefinition:
    arguments: type[StrictModel]
    result: type[StrictModel]
    handler: Callable[..., Any]
    capability: Capability = Capability.READ
    write: bool = False
