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

from llmin.domain import (
    ContractModel,
    Evidence,
    ExecutionPlan,
    TaskSpec,
    VerificationReport,
    VerificationVerdict,
)
from llmin.domain.json_types import FrozenDict, freeze_json_object
from llmin.execution import ExecutionReport
from llmin.observability import TraceEvent, redact
from llmin.orchestrator import TaskState


def artifact_content_hash(content: str) -> str:
    sanitized = redact(content)
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


episode_content_hash = artifact_content_hash


def environment_content_hash(attributes: dict[str, Any]) -> str:
    sanitized = redact(attributes)
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


class ArtifactKind(StrEnum):
    EPISODE = "episode"
    RULE = "rule"
    EXPERIMENT = "experiment"
    COMPILED_SKILL = "compiled_skill"


class ActivationState(StrEnum):
    CANDIDATE = "candidate"
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class RelationKind(StrEnum):
    DERIVED_FROM = "derived_from"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    REFINES = "refines"
    SUPERSEDES = "supersedes"
    TESTS = "tests"


class ExperimentStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class ContradictionStatus(StrEnum):
    OPEN = "open"
    EXPLAINED = "explained"
    INVALIDATED = "invalidated"


class Applicability(ContractModel):
    family: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    scope: dict[str, Any] = Field(default_factory=dict)
    environment_fingerprints: frozenset[str] = frozenset()
    preconditions: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    required_capabilities: frozenset[str] = frozenset()

    @field_validator("scope", mode="after")
    @classmethod
    def freeze_scope(cls, value: Any) -> FrozenDict:
        return freeze_json_object(value)

    @field_validator("environment_fingerprints")
    @classmethod
    def validate_environment_fingerprints(cls, value: frozenset[str]) -> frozenset[str]:
        if any(
            len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
            for item in value
        ):
            raise ValueError("environment fingerprints must be lowercase SHA-256 values")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def validate_capabilities(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not item or not item.replace("_", "").isalnum() for item in value):
            raise ValueError("required capabilities must be identifiers")
        return value

    @model_validator(mode="after")
    def reject_conflicting_conditions(self) -> Self:
        if set(self.preconditions) & set(self.exclusions):
            raise ValueError("the same condition cannot be required and excluded")
        return self


class Provenance(ContractModel):
    source_event_ids: frozenset[UUID] = frozenset()
    evidence_ids: frozenset[UUID] = frozenset()
    verification_report_ids: frozenset[UUID] = frozenset()
    parent_artifact_ids: frozenset[UUID] = frozenset()

    @model_validator(mode="after")
    def require_source(self) -> Self:
        if not (
            self.source_event_ids
            or self.evidence_ids
            or self.verification_report_ids
            or self.parent_artifact_ids
        ):
            raise ValueError("provenance requires at least one source")
        return self


class CostCategory(StrEnum):
    LLM = "llm"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    STORAGE = "storage"
    RETRIEVAL = "retrieval"
    REVALIDATION = "revalidation"
    DELETION = "deletion"


class AttemptStatus(StrEnum):
    OPEN = "open"
    FINALIZED = "finalized"


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


class MemoryArtifact(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    artifact_id: UUID = Field(default_factory=uuid4)
    kind: ArtifactKind
    state: MemoryState = MemoryState.ACTIVE
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: Provenance
    applicability: Applicability
    retention: RetentionPolicy
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class Episode(MemoryArtifact):
    kind: Literal[ArtifactKind.EPISODE] = ArtifactKind.EPISODE
    layer: Literal[MemoryLayer.EPISODIC] = MemoryLayer.EPISODIC
    task_id: UUID
    attempt_id: UUID
    summary: str | None = Field(default=None, max_length=8_000)

    @property
    def episode_id(self) -> UUID:
        return self.artifact_id

    @model_validator(mode="after")
    def validate_episode(self) -> Self:
        if self.state is MemoryState.TOMBSTONED and self.summary is not None:
            raise ValueError("tombstoned episodes cannot retain summary payload")
        if self.state is not MemoryState.TOMBSTONED and not self.summary:
            raise ValueError("non-tombstoned episodes require a summary")
        if not self.provenance.source_event_ids or not self.provenance.evidence_ids:
            raise ValueError("episodes require trace and evidence provenance")
        return self


class RuleArtifact(MemoryArtifact):
    kind: Literal[ArtifactKind.RULE] = ArtifactKind.RULE
    activation_state: ActivationState = ActivationState.CANDIDATE
    statement: str = Field(min_length=1, max_length=8_000)
    verifier_suite: tuple[str, ...] = Field(min_length=1)


class ExperimentArtifact(MemoryArtifact):
    kind: Literal[ArtifactKind.EXPERIMENT] = ArtifactKind.EXPERIMENT
    hypothesis: str = Field(min_length=1, max_length=8_000)
    method: str = Field(min_length=1, max_length=8_000)
    outcome: str | None = Field(default=None, max_length=8_000)
    status: ExperimentStatus
    rejection_reason: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def validate_experiment_result(self) -> Self:
        if self.status is ExperimentStatus.REJECTED and not self.rejection_reason:
            raise ValueError("rejected experiments require a rejection reason")
        if (
            self.status
            in {
                ExperimentStatus.CONFIRMED,
                ExperimentStatus.REJECTED,
                ExperimentStatus.INCONCLUSIVE,
            }
            and not self.outcome
        ):
            raise ValueError("completed experiments require an outcome")
        return self


class ArtifactRelation(ContractModel):
    relation_id: UUID = Field(default_factory=uuid4)
    source_artifact_id: UUID
    target_artifact_id: UUID
    kind: RelationKind
    reason: str = Field(min_length=1, max_length=2_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def reject_self_relation(self) -> Self:
        if self.source_artifact_id == self.target_artifact_id:
            raise ValueError("artifact cannot relate to itself")
        return self


class ArtifactVerifierResult(ContractModel):
    result_id: UUID = Field(default_factory=uuid4)
    artifact_id: UUID
    verification_report_id: UUID
    verdict: VerificationVerdict
    verifier_version: str = Field(min_length=1)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class ContradictionRecord(ContractModel):
    contradiction_id: UUID = Field(default_factory=uuid4)
    artifact_ids: frozenset[UUID] = Field(min_length=2)
    applicability: Applicability
    status: ContradictionStatus = ContradictionStatus.OPEN
    description: str = Field(min_length=1, max_length=4_000)
    resolution: str | None = Field(default=None, max_length=4_000)
    supersedes_contradiction_id: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        if self.status is ContradictionStatus.OPEN and self.resolution is not None:
            raise ValueError("open contradictions cannot claim a resolution")
        if self.status is ContradictionStatus.OPEN and self.supersedes_contradiction_id is not None:
            raise ValueError("open contradictions cannot supersede a prior investigation")
        if self.status is not ContradictionStatus.OPEN and not self.resolution:
            raise ValueError("closed contradictions require a resolution")
        if self.status is not ContradictionStatus.OPEN and self.supersedes_contradiction_id is None:
            raise ValueError("closed contradictions must supersede an open investigation")
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


class EnvironmentRecord(ContractModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    attributes: dict[str, Any]

    @field_validator("attributes", mode="after")
    @classmethod
    def freeze_attributes(cls, value: Any) -> FrozenDict:
        return freeze_json_object(value)

    @model_validator(mode="after")
    def validate_fingerprint(self) -> Self:
        if self.fingerprint != environment_content_hash(dict(self.attributes)):
            raise ValueError("environment fingerprint does not match attributes")
        return self


class ArtifactBlob(ContractModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    logical_name: str = Field(min_length=1, max_length=1_000)


class AttemptRecord(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    attempt_id: UUID
    trace_id: UUID
    task: TaskSpec
    plan: ExecutionPlan | None = None
    environment: EnvironmentRecord
    status: AttemptStatus = AttemptStatus.OPEN
    final_state: TaskState | None = None
    execution_report: ExecutionReport | None = None
    verification_report: VerificationReport | None = None
    artifacts: tuple[ArtifactBlob, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finalized_at: datetime | None = None

    @field_validator("created_at", "finalized_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("attempt timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_identity_and_state(self) -> Self:
        if self.plan is not None and self.plan.task_id != self.task.task_id:
            raise ValueError("attempt plan belongs to another task")
        if self.execution_report is not None:
            if self.execution_report.task_id != self.task.task_id:
                raise ValueError("execution report belongs to another task")
            if self.plan is None or self.execution_report.plan_id != self.plan.plan_id:
                raise ValueError("execution report belongs to another plan")
        if self.verification_report is not None:
            if self.verification_report.task_id != self.task.task_id:
                raise ValueError("verification report belongs to another task")
            if self.verification_report.attempt_id != self.attempt_id:
                raise ValueError("verification report belongs to another attempt")
            if self.execution_report is None or not self.execution_report.success:
                raise ValueError("verification report requires successful execution")
        if self.status is AttemptStatus.OPEN:
            if (
                any(
                    value is not None
                    for value in (
                        self.final_state,
                        self.execution_report,
                        self.verification_report,
                        self.finalized_at,
                    )
                )
                or self.artifacts
            ):
                raise ValueError("open attempts cannot contain finalized outputs")
        elif self.final_state is None or self.finalized_at is None:
            raise ValueError("finalized attempts require final state and timestamp")
        if self.final_state is TaskState.COMPLETED:
            if self.execution_report is None or not self.execution_report.success:
                raise ValueError("completed attempts require successful execution")
            if (
                self.verification_report is None
                or self.verification_report.verdict is not VerificationVerdict.PASSED
            ):
                raise ValueError("completed attempts require passed verification")
        return self
