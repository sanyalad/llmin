"""Bounded executor for allowlisted typed capabilities."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from uuid import UUID

from llmin.domain import Action, ExecutionPlan, TaskSpec
from llmin.execution.capabilities import (
    Capability,
    CapabilityError,
    PatchTomlCapability,
    ReadTextCapability,
    WriteTextAtomicCapability,
)
from llmin.execution.models import ActionResult, ExecutionReport
from llmin.execution.sandbox import SandboxFactory, SandboxPolicyError
from llmin.observability.trace import TraceEvent, TraceSink


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    @classmethod
    def with_builtins(cls) -> CapabilityRegistry:
        registry = cls()
        registry.register(ReadTextCapability())
        registry.register(WriteTextAtomicCapability())
        registry.register(PatchTomlCapability())
        return registry

    def register(self, capability: Capability) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"capability is already registered: {capability.name}")
        self._capabilities[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)


class _ActionFailure(RuntimeError):
    def __init__(self, *, safe_message: str, error_type: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.error_type = error_type


class Executor:
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        sandbox_factory: SandboxFactory,
        trace_sink: TraceSink,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._registry = registry
        self._sandbox_factory = sandbox_factory
        self._trace_sink = trace_sink
        self._clock = clock

    def authorize(self, *, task: TaskSpec, plan: ExecutionPlan) -> str | None:
        if plan.task_id != task.task_id:
            return "plan task_id does not match TaskSpec"
        if len(plan.actions) > task.budget.max_actions:
            return "plan exceeds the action budget"
        for action in plan.actions:
            if action.capability not in task.constraints.allowed_capabilities:
                return f"capability is not authorized by TaskSpec: {action.capability}"
            if self._registry.get(action.capability) is None:
                return f"capability is not registered: {action.capability}"
        return None

    def execute(
        self,
        *,
        task: TaskSpec,
        plan: ExecutionPlan,
        trace_id: UUID,
        attempt_id: UUID,
    ) -> ExecutionReport:
        started = self._clock()
        authorization_error = self.authorize(task=task, plan=plan)
        if authorization_error is not None:
            self._emit(
                task=task,
                trace_id=trace_id,
                attempt_id=attempt_id,
                event_type="execution.preflight_rejected",
                payload={"reason": authorization_error},
            )
            return self._failure_report(
                task=task,
                plan=plan,
                started=started,
                error=authorization_error,
                error_type="AuthorizationError",
            )

        try:
            sandbox = self._sandbox_factory.for_execution(task)
        except SandboxPolicyError as error:
            self._emit(
                task=task,
                trace_id=trace_id,
                attempt_id=attempt_id,
                event_type="execution.preflight_rejected",
                payload={"reason": str(error)},
            )
            return self._failure_report(
                task=task,
                plan=plan,
                started=started,
                error=str(error),
                error_type=type(error).__name__,
            )

        self._emit(
            task=task,
            trace_id=trace_id,
            attempt_id=attempt_id,
            event_type="execution.preflight_authorized",
            payload={
                "plan_id": str(plan.plan_id),
                "action_count": len(plan.actions),
                "workspace": task.workspace,
            },
        )

        transaction = sandbox.transaction()
        results: list[ActionResult] = []
        try:
            with transaction:
                for sequence, action in enumerate(plan.actions, start=1):
                    capability = self._registry.get(action.capability)
                    if capability is None:
                        raise _ActionFailure(
                            safe_message=f"capability is not registered: {action.capability}",
                            error_type="CapabilityNotRegistered",
                        )
                    self._emit(
                        task=task,
                        trace_id=trace_id,
                        attempt_id=attempt_id,
                        event_type="execution.action_started",
                        payload={
                            "action_id": str(action.action_id),
                            "capability": action.capability,
                            "sequence": sequence,
                        },
                    )
                    action_started = self._clock()
                    try:
                        output = capability.execute(action.arguments, transaction)
                    except CapabilityError as error:
                        failure = _ActionFailure(
                            safe_message=str(error),
                            error_type=type(error).__name__,
                        )
                        self._record_action_failure(
                            task=task,
                            action=action,
                            failure=failure,
                            trace_id=trace_id,
                            attempt_id=attempt_id,
                            sequence=sequence,
                            started=action_started,
                            results=results,
                        )
                        raise failure from error
                    except Exception as error:
                        failure = _ActionFailure(
                            safe_message="unexpected capability failure",
                            error_type=type(error).__name__,
                        )
                        self._record_action_failure(
                            task=task,
                            action=action,
                            failure=failure,
                            trace_id=trace_id,
                            attempt_id=attempt_id,
                            sequence=sequence,
                            started=action_started,
                            results=results,
                        )
                        raise failure from error

                    elapsed_ms = (self._clock() - action_started) * 1_000
                    results.append(
                        ActionResult(
                            action_id=action.action_id,
                            capability=action.capability,
                            success=True,
                            output=output,
                        )
                    )
                    self._emit(
                        task=task,
                        trace_id=trace_id,
                        attempt_id=attempt_id,
                        event_type="execution.action_completed",
                        payload={
                            "action_id": str(action.action_id),
                            "capability": action.capability,
                            "sequence": sequence,
                            "elapsed_ms": elapsed_ms,
                        },
                    )

                changes = transaction.commit()
        except Exception as error:
            safe_message = "unexpected execution failure"
            error_type = type(error).__name__
            if isinstance(error, _ActionFailure):
                safe_message = error.safe_message
                error_type = error.error_type
            self._emit(
                task=task,
                trace_id=trace_id,
                attempt_id=attempt_id,
                event_type=(
                    "execution.rollback_completed"
                    if transaction.rolled_back
                    else "execution.rollback_failed"
                ),
                payload={"error_type": error_type},
            )
            return self._failure_report(
                task=task,
                plan=plan,
                started=started,
                error=safe_message,
                error_type=error_type,
                action_results=tuple(results),
                rolled_back=transaction.rolled_back,
            )

        elapsed_ms = (self._clock() - started) * 1_000
        self._emit(
            task=task,
            trace_id=trace_id,
            attempt_id=attempt_id,
            event_type="execution.completed",
            payload={
                "plan_id": str(plan.plan_id),
                "elapsed_ms": elapsed_ms,
                "cost_usd": 0,
                "changes": [change.model_dump(mode="json") for change in changes],
            },
        )
        return ExecutionReport(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            success=True,
            action_results=tuple(results),
            changes=changes,
            elapsed_ms=elapsed_ms,
        )

    def _record_action_failure(
        self,
        *,
        task: TaskSpec,
        action: Action,
        failure: _ActionFailure,
        trace_id: UUID,
        attempt_id: UUID,
        sequence: int,
        started: float,
        results: list[ActionResult],
    ) -> None:
        action_id = action.action_id
        capability = action.capability
        results.append(
            ActionResult(
                action_id=action_id,
                capability=capability,
                success=False,
                error=failure.safe_message,
                error_type=failure.error_type,
            )
        )
        self._emit(
            task=task,
            trace_id=trace_id,
            attempt_id=attempt_id,
            event_type="execution.action_failed",
            payload={
                "action_id": str(action_id),
                "capability": capability,
                "sequence": sequence,
                "elapsed_ms": (self._clock() - started) * 1_000,
                "error_type": failure.error_type,
            },
        )

    def _failure_report(
        self,
        *,
        task: TaskSpec,
        plan: ExecutionPlan,
        started: float,
        error: str,
        error_type: str,
        action_results: tuple[ActionResult, ...] = (),
        rolled_back: bool = False,
    ) -> ExecutionReport:
        return ExecutionReport(
            task_id=task.task_id,
            plan_id=plan.plan_id,
            success=False,
            action_results=action_results,
            rolled_back=rolled_back,
            error=error,
            error_type=error_type,
            elapsed_ms=(self._clock() - started) * 1_000,
        )

    def _emit(
        self,
        *,
        task: TaskSpec,
        trace_id: UUID,
        attempt_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        self._trace_sink.emit(
            TraceEvent(
                trace_id=trace_id,
                task_id=task.task_id,
                attempt_id=attempt_id,
                event_type=event_type,
                payload=payload,
            )
        )
