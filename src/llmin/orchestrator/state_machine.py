"""Finite-state orchestration with auditable, bounded transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from llmin.domain.models import ContractModel
from llmin.observability.trace import TraceEvent, TraceSink


class TaskState(StrEnum):
    RECEIVED = "received"
    ROUTED = "routed"
    PLANNED = "planned"
    AUTHORIZED = "authorized"
    EXECUTED = "executed"
    VERIFIED = "verified"
    RECORDED = "recorded"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"


TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.ESCALATED})

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.RECEIVED: frozenset({TaskState.ROUTED, TaskState.FAILED, TaskState.ESCALATED}),
    TaskState.ROUTED: frozenset({TaskState.PLANNED, TaskState.FAILED, TaskState.ESCALATED}),
    TaskState.PLANNED: frozenset({TaskState.AUTHORIZED, TaskState.FAILED, TaskState.ESCALATED}),
    TaskState.AUTHORIZED: frozenset({TaskState.EXECUTED, TaskState.FAILED}),
    TaskState.EXECUTED: frozenset({TaskState.VERIFIED, TaskState.FAILED}),
    TaskState.VERIFIED: frozenset({TaskState.RECORDED, TaskState.FAILED}),
    TaskState.RECORDED: frozenset({TaskState.COMPLETED, TaskState.FAILED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.ESCALATED: frozenset(),
}


class InvalidTransitionError(ValueError):
    pass


class TransitionRecord(ContractModel):
    sequence: int = Field(ge=1)
    from_state: TaskState
    to_state: TaskState
    reason: str = Field(min_length=1, max_length=1_000)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value


class OrchestratorRun:
    """Mutable runtime state whose externally stored records remain immutable."""

    def __init__(
        self,
        *,
        task_id: UUID,
        trace_sink: TraceSink,
        trace_id: UUID | None = None,
        attempt_id: UUID | None = None,
    ) -> None:
        self.task_id = task_id
        self.trace_id = trace_id or uuid4()
        self.attempt_id = attempt_id or uuid4()
        self.state = TaskState.RECEIVED
        self.history: list[TransitionRecord] = []
        self._trace_sink = trace_sink

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition(self, target: TaskState, *, reason: str) -> TransitionRecord:
        allowed = ALLOWED_TRANSITIONS[self.state]
        if target not in allowed:
            raise InvalidTransitionError(
                f"transition {self.state.value} -> {target.value} is not allowed"
            )

        record = TransitionRecord(
            sequence=len(self.history) + 1,
            from_state=self.state,
            to_state=target,
            reason=reason,
        )
        event = TraceEvent(
            trace_id=self.trace_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            event_type="orchestrator.transition",
            timestamp=record.occurred_at,
            payload={
                "sequence": record.sequence,
                "from_state": record.from_state.value,
                "to_state": record.to_state.value,
                "reason": record.reason,
            },
        )

        self._trace_sink.emit(event)
        self.history.append(record)
        self.state = target
        return record
