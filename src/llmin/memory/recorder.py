"""Atomic persistence boundary for completed pipeline attempts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from uuid import UUID, uuid4

from llmin.domain import TaskSpec
from llmin.domain.json_types import canonical_sha256
from llmin.memory.artifacts import ContentAddressedArtifactStore
from llmin.memory.models import (
    AttemptRecord,
    AttemptStatus,
    EnvironmentRecord,
    RecordingReceipt,
    environment_content_hash,
)
from llmin.memory.sqlite import MemoryStoreError, SQLiteMemoryStore
from llmin.observability import redact
from llmin.orchestrator import TaskState
from llmin.pipeline import Pipeline, PipelineResult


@dataclass(frozen=True)
class CoordinatedAttempt:
    """The runtime result and durable record of one coordinated attempt."""

    result: PipelineResult
    record: AttemptRecord
    receipt: RecordingReceipt


ArtifactCollector = Callable[
    [PipelineResult],
    Mapping[str, tuple[bytes, str]],
]


class AttemptRecorder:
    def __init__(
        self,
        *,
        memory: SQLiteMemoryStore,
        artifacts: ContentAddressedArtifactStore,
    ) -> None:
        self._memory = memory
        self._artifacts = artifacts

    def record(
        self,
        *,
        task: TaskSpec,
        result: PipelineResult,
        environment_attributes: dict[str, object],
        artifact_payloads: Mapping[str, tuple[bytes, str]] | None = None,
    ) -> AttemptRecord:
        record, _receipt = self.record_with_receipt(
            task=task,
            result=result,
            environment_attributes=environment_attributes,
            artifact_payloads=artifact_payloads,
        )
        return record

    def record_with_receipt(
        self,
        *,
        task: TaskSpec,
        result: PipelineResult,
        environment_attributes: dict[str, object],
        artifact_payloads: Mapping[str, tuple[bytes, str]] | None = None,
    ) -> tuple[AttemptRecord, RecordingReceipt]:
        if result.task_id != task.task_id:
            raise MemoryStoreError("pipeline result belongs to another task")
        if result.final_state not in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.ESCALATED,
        }:
            raise MemoryStoreError("only a terminal pipeline result can finalize an attempt")
        sanitized_environment = redact(environment_attributes)
        environment = EnvironmentRecord(
            fingerprint=environment_content_hash(sanitized_environment),
            attributes=sanitized_environment,
        )
        existing = self._memory.get_attempt(result.attempt_id)
        if existing is None:
            self._memory.begin_attempt(
                attempt_id=result.attempt_id,
                trace_id=result.trace_id,
                task=task,
                plan=result.execution_plan,
                environment=environment,
            )
        else:
            self._validate_retry(existing, task, result, environment)

        blobs = tuple(
            self._artifacts.put(content, logical_name=name, media_type=media_type)
            for name, (content, media_type) in sorted((artifact_payloads or {}).items())
        )
        return self._memory.finalize_attempt_with_receipt(
            result.attempt_id,
            plan=result.execution_plan,
            final_state=result.final_state,
            execution_report=result.execution_report,
            verification_report=result.verification_report,
            artifacts=blobs,
        )

    @staticmethod
    def _validate_retry(
        existing: AttemptRecord,
        task: TaskSpec,
        result: PipelineResult,
        environment: EnvironmentRecord,
    ) -> None:
        if (
            existing.task != TaskSpec.model_validate(redact(task.model_dump()))
            or existing.trace_id != result.trace_id
            or existing.environment != environment
        ):
            raise MemoryStoreError("attempt identifier was reused with different inputs")
        if existing.plan is not None:
            sanitized_plan = (
                type(result.execution_plan).model_validate(
                    redact(result.execution_plan.model_dump())
                )
                if result.execution_plan is not None
                else None
            )
            if existing.plan != sanitized_plan:
                raise MemoryStoreError("attempt identifier was reused with different inputs")
        if existing.status not in {AttemptStatus.OPEN, AttemptStatus.FINALIZED}:
            raise MemoryStoreError("attempt has an unsupported persistence state")


class AttemptCoordinator:
    """Create the durable attempt envelope before any pipeline event is emitted."""

    def __init__(
        self,
        *,
        memory: SQLiteMemoryStore,
        artifacts: ContentAddressedArtifactStore,
    ) -> None:
        self._memory = memory
        self._recorder = AttemptRecorder(memory=memory, artifacts=artifacts)

    def run(
        self,
        *,
        pipeline: Pipeline,
        task: TaskSpec,
        environment_attributes: dict[str, object],
        artifact_collector: ArtifactCollector | None = None,
        trace_id: UUID | None = None,
        attempt_id: UUID | None = None,
    ) -> CoordinatedAttempt:
        """Persist an open attempt, run it, then finalize it with the observed result.

        If execution raises before a PipelineResult exists, the open attempt remains available
        for recovery and diagnosis.
        """

        selected_trace_id = trace_id or uuid4()
        selected_attempt_id = attempt_id or uuid4()
        environment = self._environment(environment_attributes)
        existing = self._memory.get_attempt(selected_attempt_id)
        if existing is None:
            self._memory.begin_attempt(
                attempt_id=selected_attempt_id,
                trace_id=selected_trace_id,
                task=task,
                plan=None,
                environment=environment,
            )
        else:
            self._validate_prepared_attempt(existing, task, selected_trace_id, environment)
            raise MemoryStoreError("an existing attempt cannot be re-executed")

        result = pipeline.run(
            task,
            trace_id=selected_trace_id,
            attempt_id=selected_attempt_id,
        )
        if (
            result.task_id != task.task_id
            or result.trace_id != selected_trace_id
            or result.attempt_id != selected_attempt_id
        ):
            raise MemoryStoreError("pipeline result does not match the prepared attempt")
        terminal_result = (
            replace(result, final_state=TaskState.COMPLETED)
            if result.final_state is TaskState.VERIFIED
            else result
        )
        artifact_payloads = (
            artifact_collector(terminal_result)
            if artifact_collector is not None and terminal_result.final_state is TaskState.COMPLETED
            else None
        )
        recorded, receipt = self._recorder.record_with_receipt(
            task=task,
            result=terminal_result,
            environment_attributes=environment_attributes,
            artifact_payloads=artifact_payloads,
        )
        self._validate_receipt(recorded, receipt)
        return CoordinatedAttempt(
            result=terminal_result,
            record=recorded,
            receipt=receipt,
        )

    @staticmethod
    def _environment(attributes: dict[str, object]) -> EnvironmentRecord:
        sanitized = redact(attributes)
        return EnvironmentRecord(
            fingerprint=environment_content_hash(sanitized),
            attributes=sanitized,
        )

    @staticmethod
    def _validate_prepared_attempt(
        existing: AttemptRecord,
        task: TaskSpec,
        trace_id: UUID,
        environment: EnvironmentRecord,
    ) -> None:
        if (
            existing.task != TaskSpec.model_validate(redact(task.model_dump()))
            or existing.trace_id != trace_id
            or existing.environment != environment
        ):
            raise MemoryStoreError("attempt identifier was reused with different inputs")

    @staticmethod
    def _validate_receipt(
        record: AttemptRecord,
        receipt: RecordingReceipt,
    ) -> None:
        if (
            receipt.attempt_id != record.attempt_id
            or receipt.trace_id != record.trace_id
            or receipt.task_id != record.task.task_id
            or receipt.record_status is not AttemptStatus.FINALIZED
            or receipt.document_hash != canonical_sha256(record)
        ):
            raise MemoryStoreError("recording receipt does not match finalized attempt")
