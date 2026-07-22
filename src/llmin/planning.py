"""Provider-neutral planning boundary with a deterministic test planner."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from llmin.domain import ExecutionPlan, TaskSpec


class Planner(Protocol):
    def plan(self, task: TaskSpec) -> ExecutionPlan: ...


class FakePlanner:
    """Deterministic planner used by fixtures and end-to-end tests."""

    def __init__(self, factory: Callable[[TaskSpec], ExecutionPlan]) -> None:
        self._factory = factory

    def plan(self, task: TaskSpec) -> ExecutionPlan:
        return self._factory(task)
