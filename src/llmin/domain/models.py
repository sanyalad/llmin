"""Versioned, serializable contracts for the Stage 1 execution pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _safe_relative_path(value: str) -> str:
    """Normalize a portable relative path and reject sandbox escapes."""

    if not value or "\\" in value:
        raise ValueError("path must be a non-empty portable path using '/' separators")

    path = PurePosixPath(value)
    normalized = path.as_posix()
    if (
        path.is_absolute()
        or not path.parts
        or normalized != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("path must be normalized, relative, and remain inside the sandbox")
    if ":" in path.parts[0]:
        raise ValueError("drive-qualified paths are not allowed")
    return normalized


class ContractModel(BaseModel):
    """Base class for immutable external contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PlannerKind(StrEnum):
    COMPILED = "compiled"
    HEURISTIC = "heuristic"
    LLM = "llm"
    FAKE = "fake"


class VerificationVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class KnowledgeStatus(StrEnum):
    CANDIDATE = "candidate"
    HYPOTHESIS = "hypothesis"
    HEURISTIC = "heuristic"
    COMPILED = "compiled"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class Budget(ContractModel):
    max_llm_calls: int = Field(default=2, ge=0)
    max_cost_usd: Decimal = Field(default=Decimal("0.10"), ge=0)
    timeout_seconds: int = Field(default=30, gt=0, le=3600)
    max_actions: int = Field(default=20, gt=0, le=10_000)


class TaskConstraints(ContractModel):
    writable_paths: tuple[str, ...] = ()
    allowed_capabilities: frozenset[str] = frozenset()
    network_allowed: bool = False

    @field_validator("writable_paths", mode="before")
    @classmethod
    def validate_writable_paths(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(_safe_relative_path(item) for item in value)

    @field_validator("allowed_capabilities")
    @classmethod
    def validate_capabilities(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not capability or not capability.replace("_", "").isalnum() for capability in value):
            raise ValueError("capabilities must be non-empty identifiers")
        return value


class Postcondition(ContractModel):
    type: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    parameters: dict[str, Any] = Field(default_factory=dict)
    required: bool = True


class TaskSpec(ContractModel):
    schema_version: str = "1.0"
    task_id: UUID = Field(default_factory=uuid4)
    family: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    objective: str = Field(min_length=1, max_length=4_000)
    workspace: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    postconditions: tuple[Postcondition, ...] = Field(min_length=1)
    risk_class: RiskClass = RiskClass.LOW
    budget: Budget = Field(default_factory=Budget)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: str) -> str:
        return _safe_relative_path(value)

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class Action(ContractModel):
    action_id: UUID = Field(default_factory=uuid4)
    capability: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=10, gt=0, le=600)


class ExecutionPlan(ContractModel):
    schema_version: str = "1.0"
    plan_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    planner_kind: PlannerKind
    actions: tuple[Action, ...] = Field(min_length=1)
    knowledge_artifact_id: UUID | None = None
    estimated_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def validate_knowledge_source(self) -> Self:
        if self.planner_kind in {PlannerKind.COMPILED, PlannerKind.HEURISTIC}:
            if self.knowledge_artifact_id is None:
                raise ValueError("known-knowledge plans require knowledge_artifact_id")
        elif self.knowledge_artifact_id is not None:
            raise ValueError("LLM and fake plans cannot claim a knowledge artifact")
        return self


class Evidence(ContractModel):
    evidence_id: UUID = Field(default_factory=uuid4)
    kind: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    locator: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class VerificationReport(ContractModel):
    report_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    attempt_id: UUID
    verdict: VerificationVerdict
    covered_postconditions: frozenset[int] = frozenset()
    evidence: tuple[Evidence, ...] = ()
    errors: tuple[str, ...] = ()
    verifier_version: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_verdict_details(self) -> Self:
        if self.verdict is VerificationVerdict.PASSED and self.errors:
            raise ValueError("a passed report cannot contain errors")
        if self.verdict is not VerificationVerdict.PASSED and not self.errors:
            raise ValueError("failed or inconclusive reports must explain the result")
        if any(index < 0 for index in self.covered_postconditions):
            raise ValueError("postcondition indexes cannot be negative")
        return self


class KnowledgeArtifact(ContractModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    version: int = Field(default=1, gt=0)
    family: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    status: KnowledgeStatus = KnowledgeStatus.CANDIDATE
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_attempt_ids: frozenset[UUID] = Field(min_length=1)
    required_capabilities: frozenset[str] = frozenset()
    preconditions: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    verifier_suite: tuple[str, ...] = Field(min_length=1)
    fallback_route: PlannerKind = PlannerKind.LLM
    reliability_successes: int = Field(default=0, ge=0)
    reliability_attempts: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_reliability(self) -> Self:
        if self.reliability_successes > self.reliability_attempts:
            raise ValueError("successes cannot exceed attempts")
        if self.fallback_route is PlannerKind.COMPILED:
            raise ValueError("fallback cannot point to another compiled route")
        return self
