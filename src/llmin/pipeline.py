"""Minimal TaskSpec-to-terminal-state Stage 1 pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from llmin.domain import ExecutionPlan, TaskSpec, VerificationReport, VerificationVerdict
from llmin.execution import ExecutionReport, Executor
from llmin.observability.trace import TraceEvent, TraceSink
from llmin.orchestrator import OrchestratorRun, TaskState
from llmin.planning import Planner
from llmin.verification import VerificationService


@dataclass(frozen=True)
class PipelineResult:
    trace_id: UUID
    attempt_id: UUID
    final_state: TaskState
    execution_plan: ExecutionPlan | None
    execution_report: ExecutionReport | None
    verification_report: VerificationReport | None


class Pipeline:
    def __init__(
        self,
        *,
        planner: Planner,
        executor: Executor,
        verification: VerificationService,
        trace_sink: TraceSink,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._verification = verification
        self._trace_sink = trace_sink

    def run(self, task: TaskSpec) -> PipelineResult:
        run = OrchestratorRun(task_id=task.task_id, trace_sink=self._trace_sink)
        run.transition(TaskState.ROUTED, reason="task accepted for routing")
        try:
            plan = self._planner.plan(task)
        except Exception as error:
            self._trace_sink.emit(
                TraceEvent(
                    trace_id=run.trace_id,
                    task_id=task.task_id,
                    attempt_id=run.attempt_id,
                    event_type="planning.failed",
                    payload={"error_type": type(error).__name__},
                )
            )
            run.transition(TaskState.FAILED, reason="planner failed")
            return PipelineResult(run.trace_id, run.attempt_id, run.state, None, None, None)

        run.transition(TaskState.PLANNED, reason="planner produced a structured plan")
        authorization_error = self._executor.authorize(task=task, plan=plan)
        if authorization_error is not None:
            run.transition(TaskState.FAILED, reason="plan authorization failed")
            return PipelineResult(run.trace_id, run.attempt_id, run.state, plan, None, None)
        run.transition(TaskState.AUTHORIZED, reason="plan passed capability policy")

        try:
            execution = self._executor.execute(
                task=task,
                plan=plan,
                trace_id=run.trace_id,
                attempt_id=run.attempt_id,
            )
        except Exception:
            run.transition(TaskState.FAILED, reason="executor failed unexpectedly")
            return PipelineResult(run.trace_id, run.attempt_id, run.state, plan, None, None)
        if not execution.success:
            run.transition(TaskState.FAILED, reason="execution failed")
            return PipelineResult(run.trace_id, run.attempt_id, run.state, plan, execution, None)
        run.transition(TaskState.EXECUTED, reason="all actions returned successfully")

        try:
            verification = self._verification.verify(
                task=task,
                trace_id=run.trace_id,
                attempt_id=run.attempt_id,
            )
        except Exception:
            run.transition(TaskState.FAILED, reason="verifier failed unexpectedly")
            return PipelineResult(run.trace_id, run.attempt_id, run.state, plan, execution, None)
        if verification.verdict is not VerificationVerdict.PASSED:
            run.transition(TaskState.FAILED, reason="independent verification did not pass")
            return PipelineResult(
                run.trace_id,
                run.attempt_id,
                run.state,
                plan,
                execution,
                verification,
            )

        run.transition(TaskState.VERIFIED, reason="independent verifier passed")
        run.transition(TaskState.RECORDED, reason="trace and evidence emitted")
        run.transition(TaskState.COMPLETED, reason="task reached a verified terminal state")
        return PipelineResult(
            run.trace_id,
            run.attempt_id,
            run.state,
            plan,
            execution,
            verification,
        )
