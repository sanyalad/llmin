"""Contracts for governed memory and attempt reconstruction."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from llmin.domain import ContractModel, Evidence
from llmin.domain.json_types import FrozenDict, freeze_json_object
from llmin.observability import TraceEvent, redact


def episode_content_hash(summary: str) -> str:
    sanitized = redact(summary)
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MemoryLayer(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryState(StrEnum):
    ACTIVE = "active"
    COLD = "cold"
    QUARANTINED = "quarantined"
    TOMBSTONED = "tombstoned"


class CostCategory(StrEnum):
    LLM = "llm"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    STORAGE = "storage"
    RETRIEVAL = "retrieval"
    REVALIDATION = "revalidation"
    DELETION = "deletion"


class RetentionPolicy(ContractModel):
    minimum_retain_until: datetime
    expires_at: datetime | None = None
    reason: str = Field(min_length=1, max_length=1_000)

    @field_validator("minimum_retain_until", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("retention timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.expires_at is not None and self.expires_at < self.minimum_retain_until:
            raise ValueError("expires_at cannot precede minimum_retain_until")
        return self


class Episode(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    episode_id: UUID = Field(default_factory=uuid4)
    layer: Literal[MemoryLayer.EPISODIC] = MemoryLayer.EPISODIC
    state: MemoryState = MemoryState.ACTIVE
    task_id: UUID
    attempt_id: UUID
    summary: str | None = Field(default=None, max_length=8_000)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_event_ids: frozenset[UUID] = Field(min_length=1)
    evidence_ids: frozenset[UUID] = Field(min_length=1)
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    retention: RetentionPolicy
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_payload_lifecycle(self) -> Self:
        if self.state is MemoryState.TOMBSTONED and self.summary is not None:
            raise ValueError("tombstoned episodes cannot retain summary payload")
        if self.state is not MemoryState.TOMBSTONED and not self.summary:
            raise ValueError("non-tombstoned episodes require a summary")
        return self


class EpisodeTransition(ContractModel):
    transition_id: UUID = Field(default_factory=uuid4)
    episode_id: UUID
    from_state: MemoryState
    to_state: MemoryState
    reason: str = Field(min_length=1, max_length=2_000)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def reject_noop(self) -> Self:
        if self.from_state is self.to_state:
            raise ValueError("memory transition must change state")
        return self


class CostEntry(ContractModel):
    cost_id: UUID = Field(default_factory=uuid4)
    attempt_id: UUID
    category: CostCategory
    amount_usd: Decimal = Field(ge=0)
    units: Decimal = Field(default=Decimal("1"), gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("metadata", mode="after")
    @classmethod
    def freeze_metadata(cls, value: Any) -> FrozenDict:
        return freeze_json_object(value)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value


class AttemptMemory(ContractModel):
    attempt_id: UUID
    trace_events: tuple[TraceEvent, ...]
    evidence: tuple[Evidence, ...]
    costs: tuple[CostEntry, ...]

    @model_validator(mode="after")
    def validate_attempt_identity(self) -> Self:
        if any(event.attempt_id != self.attempt_id for event in self.trace_events):
            raise ValueError("trace event belongs to a different attempt")
        if any(cost.attempt_id != self.attempt_id for cost in self.costs):
            raise ValueError("cost entry belongs to a different attempt")
        return self
