"""Serializable results produced by sandbox execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from llmin.domain.json_types import FrozenDict, freeze_json_object
from llmin.domain.models import ContractModel


class ChangeKind(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"


class ChangeRecord(ContractModel):
    path: str
    kind: ChangeKind
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ActionResult(ContractModel):
    action_id: UUID
    capability: str
    success: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    error_type: str | None = None

    @field_validator("output", mode="after")
    @classmethod
    def freeze_output(cls, value: Any) -> FrozenDict:
        return freeze_json_object(value)

    @model_validator(mode="after")
    def validate_error(self) -> ActionResult:
        if self.success and self.error is not None:
            raise ValueError("successful actions cannot contain an error")
        if self.success and self.error_type is not None:
            raise ValueError("successful actions cannot contain an error type")
        if not self.success and not self.error:
            raise ValueError("failed actions must explain the error")
        if not self.success and not self.error_type:
            raise ValueError("failed actions must contain an error type")
        return self


class ExecutionReport(ContractModel):
    task_id: UUID
    plan_id: UUID
    success: bool
    action_results: tuple[ActionResult, ...] = ()
    changes: tuple[ChangeRecord, ...] = ()
    rolled_back: bool = False
    error: str | None = None
    error_type: str | None = None
    elapsed_ms: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> ExecutionReport:
        if self.success:
            if self.error is not None or self.error_type is not None or self.rolled_back:
                raise ValueError("successful execution cannot be rolled back or contain an error")
            if any(not result.success for result in self.action_results):
                raise ValueError("successful execution cannot contain failed actions")
        else:
            if not self.error:
                raise ValueError("failed execution must explain the error")
            if not self.error_type:
                raise ValueError("failed execution must contain an error type")
        return self
