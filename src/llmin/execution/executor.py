"""Bounded executor for allowlisted typed capabilities."""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

from llmin.domain import ExecutionPlan, TaskSpec
from llmin.execution.capabilities import (
    Capability,
    CapabilityError,
    ReadTextCapability,
    WriteTextAtomicCapability,
)
from llmin.execution.models import ActionResult, ExecutionReport
from llmin.execution.sandbox import Sandbox, SandboxPolicyError


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    @classmethod
    def with_builtins(cls) -> CapabilityRegistry:
        registry = cls()
        registry.register(ReadTextCapability())
        registry.register(WriteTextAtomicCapability())
        return registry

    def register(self, capability: Capability) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"capability is already registered: {capability.name}")
        self._capabilities[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)


class Executor:
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._registry = registry
        self._clock = clock

    def execute(
        self,
        *,
        task: TaskSpec,
        plan: ExecutionPlan,
        sandbox: Sandbox,
    ) -> ExecutionReport:
        preflight_error = self._preflight(task=task, plan=plan, sandbox=sandbox)
        if preflight_error is not None:
            return ExecutionReport(
                task_id=task.task_id,
                plan_id=plan.plan_id,
                success=False,
                error=preflight_error,
            )

        transaction = sandbox.transaction()
        results: list[ActionResult] = []
        deadline = self._clock() + task.budget.timeout_seconds
        try:
            for action in plan.actions:
                capability = self._registry.get(action.capability)
                if capability is None:
                    raise CapabilityError(f"capability is not registered: {action.capability}")
                try:
                    self._ensure_within_deadline(deadline)
                    output = capability.execute(action.arguments, transaction)
                    self._ensure_within_deadline(deadline)
                except CapabilityError as error:
                    results.append(
                        ActionResult(
                            action_id=action.action_id,
                            capability=action.capability,
                            success=False,
                            error=str(error),
                        )
                    )
                    raise
                results.append(
                    ActionResult(
                        action_id=action.action_id,
                        capability=action.capability,
                        success=True,
                        output=output,
                    )
                )

            changes = transaction.commit()
            return ExecutionReport(
                task_id=task.task_id,
                plan_id=plan.plan_id,
                success=True,
                action_results=tuple(results),
                changes=changes,
            )
        except (CapabilityError, SandboxPolicyError) as error:
            try:
                transaction.rollback()
            except SandboxPolicyError as rollback_error:
                return ExecutionReport(
                    task_id=task.task_id,
                    plan_id=plan.plan_id,
                    success=False,
                    action_results=tuple(results),
                    error=f"{error}; {rollback_error}",
                )
            return ExecutionReport(
                task_id=task.task_id,
                plan_id=plan.plan_id,
                success=False,
                action_results=tuple(results),
                rolled_back=True,
                error=str(error),
            )

    def _preflight(
        self,
        *,
        task: TaskSpec,
        plan: ExecutionPlan,
        sandbox: Sandbox,
    ) -> str | None:
        if plan.task_id != task.task_id:
            return "plan task_id does not match TaskSpec"
        if len(plan.actions) > task.budget.max_actions:
            return "plan exceeds the action budget"
        if sandbox.writable_paths != frozenset(task.constraints.writable_paths):
            return "sandbox writable policy does not match TaskSpec"
        for action in plan.actions:
            if action.capability not in task.constraints.allowed_capabilities:
                return f"capability is not authorized by TaskSpec: {action.capability}"
            if self._registry.get(action.capability) is None:
                return f"capability is not registered: {action.capability}"
        return None

    def _ensure_within_deadline(self, deadline: float) -> None:
        if self._clock() > deadline:
            raise CapabilityError("execution timeout exceeded")
