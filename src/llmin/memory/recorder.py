"""Atomic persistence boundary for completed pipeline attempts."""

from __future__ import annotations

from collections.abc import Mapping

from llmin.domain import TaskSpec
from llmin.memory.artifacts import ContentAddressedArtifactStore
from llmin.memory.models import (
    AttemptRecord,
    AttemptStatus,
    EnvironmentRecord,
    environment_content_hash,
)
from llmin.memory.sqlite import MemoryStoreError, SQLiteMemoryStore
from llmin.observability import redact
from llmin.pipeline import PipelineResult


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
        if result.task_id != task.task_id:
            raise MemoryStoreError("pipeline result belongs to another task")
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
        return self._memory.finalize_attempt(
            result.attempt_id,
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
            or existing.plan
            != (
                type(result.execution_plan).model_validate(
                    redact(result.execution_plan.model_dump())
                )
                if result.execution_plan is not None
                else None
            )
            or existing.environment != environment
        ):
            raise MemoryStoreError("attempt identifier was reused with different inputs")
        if existing.status not in {AttemptStatus.OPEN, AttemptStatus.FINALIZED}:
            raise MemoryStoreError("attempt has an unsupported persistence state")
