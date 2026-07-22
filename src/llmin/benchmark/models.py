"""Versioned benchmark manifest and report schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from llmin.domain import ContractModel, VerificationVerdict
from llmin.execution import ChangeRecord
from llmin.orchestrator import TaskState


class BenchmarkSplit(StrEnum):
    TRAIN = "train"
    EVIDENCE = "evidence"
    HOLDOUT = "holdout"


class CaseMode(StrEnum):
    PATCH_TOML = "patch_toml"
    CAPABILITY_ERROR = "capability_error"
    INCOMPATIBLE = "incompatible"


TomlScalar = str | bool | int | float


class ExpectedOutcome(ContractModel):
    final_state: TaskState
    execution_success: bool | None
    verification_verdict: VerificationVerdict | None


class BenchmarkCase(ContractModel):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    split: BenchmarkSplit
    mode: CaseMode
    initial_toml: str = ""
    key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
    action_value: TomlScalar | None = None
    expected_value: TomlScalar | None = None
    mutation_expected_rejection: bool = False
    expected: ExpectedOutcome

    @model_validator(mode="after")
    def validate_mode_fields(self) -> Self:
        if self.mode in {CaseMode.PATCH_TOML, CaseMode.CAPABILITY_ERROR}:
            if not self.initial_toml or self.key is None or self.action_value is None:
                raise ValueError("TOML cases require initial_toml, key, and action_value")
            if self.expected_value is None:
                raise ValueError("TOML cases require expected_value")
        if self.mode is CaseMode.INCOMPATIBLE and any(
            value is not None for value in (self.key, self.action_value, self.expected_value)
        ):
            raise ValueError("incompatible cases cannot define TOML values")
        if self.mutation_expected_rejection:
            if self.mode is not CaseMode.PATCH_TOML:
                raise ValueError("mutation rejection cases must execute patch_toml")
            if self.expected != ExpectedOutcome(
                final_state=TaskState.FAILED,
                execution_success=True,
                verification_verdict=VerificationVerdict.FAILED,
            ):
                raise ValueError(
                    "mutation rejection cases must execute successfully and fail verification"
                )
            if type(self.action_value) is type(self.expected_value) and (
                self.action_value == self.expected_value
            ):
                raise ValueError(
                    "mutation rejection action and expected values must differ strictly"
                )
        return self


class BenchmarkSuite(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    created_at: datetime
    cases: tuple[BenchmarkCase, ...] = Field(min_length=10)

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_suite_structure(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("benchmark case_id values must be unique")
        present_splits = {case.split for case in self.cases}
        if present_splits != set(BenchmarkSplit):
            raise ValueError("benchmark suite must contain train, evidence, and holdout cases")
        mutations = sum(case.mutation_expected_rejection for case in self.cases)
        if mutations < 2:
            raise ValueError("benchmark suite requires at least two verifier mutation cases")
        return self


class BenchmarkCaseResult(ContractModel):
    case_id: str
    split: BenchmarkSplit
    task_id: UUID
    final_state: TaskState
    execution_success: bool | None
    verification_verdict: VerificationVerdict | None
    execution_error_type: str | None
    action_error_types: tuple[str, ...]
    terminal_reason: str
    verification_errors: tuple[str, ...]
    evidence_sha256: tuple[str | None, ...]
    changes: tuple[ChangeRecord, ...]
    trace_event_types: tuple[str, ...]
    matched_expectation: bool
    mutation_expected_rejection: bool
    unsafe_acceptance: bool
    trace_events: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)


class BenchmarkMetrics(ContractModel):
    total_cases: int = Field(ge=0)
    matched_cases: int = Field(ge=0)
    completed_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    mutation_cases: int = Field(ge=0)
    safe_rejections: int = Field(ge=0)
    unsafe_acceptances: int = Field(ge=0)
    llm_calls: int = Field(default=0, ge=0)
    variable_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    mean_latency_ms: float = Field(ge=0)
    quality_gate_passed: bool


class BenchmarkReport(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_name: str
    suite_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_outcome_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int
    selected_split: BenchmarkSplit | None
    baseline_kind: Literal["deterministic_fixture"] = "deterministic_fixture"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    results: tuple[BenchmarkCaseResult, ...]
    metrics: BenchmarkMetrics

    @field_validator("started_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        return value
