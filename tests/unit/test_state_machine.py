from uuid import uuid4

import pytest

from llmin.observability import InMemoryTraceSink
from llmin.orchestrator import InvalidTransitionError, OrchestratorRun, TaskState


def test_happy_path_is_explicit_and_fully_traced() -> None:
    sink = InMemoryTraceSink()
    run = OrchestratorRun(task_id=uuid4(), trace_sink=sink)
    path = [
        TaskState.ROUTED,
        TaskState.PLANNED,
        TaskState.AUTHORIZED,
        TaskState.EXECUTED,
        TaskState.VERIFIED,
        TaskState.RECORDED,
        TaskState.COMPLETED,
    ]

    for state in path:
        run.transition(state, reason=f"advance to {state.value}")

    assert run.state is TaskState.COMPLETED
    assert run.is_terminal
    assert [record.sequence for record in run.history] == list(range(1, 8))
    assert len(sink.events) == len(path)
    assert sink.events[-1].payload["to_state"] == "completed"


def test_invalid_transition_does_not_mutate_or_emit() -> None:
    sink = InMemoryTraceSink()
    run = OrchestratorRun(task_id=uuid4(), trace_sink=sink)

    with pytest.raises(InvalidTransitionError, match="not allowed"):
        run.transition(TaskState.EXECUTED, reason="skip policy")

    assert run.state is TaskState.RECEIVED
    assert run.history == []
    assert sink.events == []


def test_terminal_state_cannot_transition() -> None:
    sink = InMemoryTraceSink()
    run = OrchestratorRun(task_id=uuid4(), trace_sink=sink)
    run.transition(TaskState.FAILED, reason="invalid input")

    with pytest.raises(InvalidTransitionError):
        run.transition(TaskState.ROUTED, reason="retry")
