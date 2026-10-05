"""Bounded, structured results and strict common inputs for assistant tools."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolResult(StrictModel):
    """A human explanation alongside machine-readable data and its practical limits."""

    summary: str
    data: dict[str, JsonValue] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    next_action: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PageInput(StrictModel):
    limit: int = Field(default=25, ge=1, le=100, description="Maximum records to return; use offset for later pages.")
    offset: int = Field(default=0, ge=0, le=100_000, description="Number of permitted records to skip.")


class CatalogInput(PageInput):
    query: str = Field(default="", max_length=100, description="Words from a field name, label, or description.")
    group: str | None = Field(default=None, max_length=50, description="Optional settings group from the catalog.")


class ObjectInput(StrictModel):
    id: int = Field(gt=0, description="An ID returned by the matching Shortlist discovery tool.")


class ChangeInput(StrictModel):
    change_id: str = Field(min_length=1, max_length=100, description="Opaque change ID returned by a plan tool.")


class ApplyInput(ChangeInput):
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        description=(
            "A new stable identifier for this apply attempt. Reuse it after a timeout; never "
            "create a new plan to retry."
        ),
    )


class OperationInput(StrictModel):
    operation_id: str = Field(min_length=1, max_length=100, description="Opaque operation ID from an apply receipt.")
