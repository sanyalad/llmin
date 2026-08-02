import json
import shutil
from pathlib import Path

from llmin.domain import (
    Action,
    ExecutionPlan,
    PlannerKind,
    Postcondition,
    TaskSpec,
    VerificationVerdict,
)
from llmin.execution import CapabilityRegistry, Executor, SandboxFactory
from llmin.observability import InMemoryTraceSink
from llmin.orchestrator import TaskState
from llmin.pipeline import Pipeline
from llmin.planning import FakePlanner
from llmin.verification import VerificationService, VerifierRegistry


def build_pipeline(
    base_root: Path,
    plan: ExecutionPlan,
    verifier_registry: VerifierRegistry | None = None,
) -> tuple[Pipeline, InMemoryTraceSink]:
    sink = InMemoryTraceSink()
    factory = SandboxFactory(base_root)
    executor = Executor(
        CapabilityRegistry.with_builtins(),
        sandbox_factory=factory,
        trace_sink=sink,
    )
    verification = VerificationService(
        verifier_registry or VerifierRegistry.with_builtins(),
        sandbox_factory=factory,
        trace_sink=sink,
    )
    return (
        Pipeline(
            planner=FakePlanner(lambda _task: plan),
            executor=executor,
            verification=verification,
            trace_sink=sink,
        ),
        sink,
    )


def load_benchmark(tmp_path: Path) -> tuple[TaskSpec, ExecutionPlan, Path]:
    task = TaskSpec.model_validate_json(
        Path("benchmarks/tasks/config_patch/001.json").read_text(encoding="utf-8")
    )
    plan = ExecutionPlan.model_validate_json(
        Path("benchmarks/plans/config_patch/001.json").read_text(encoding="utf-8")
    )
    workspace = tmp_path / task.workspace
    workspace.parent.mkdir(parents=True)
    shutil.copytree(Path("benchmarks/workspaces/config-patch-001"), workspace)
    return task, plan, workspace


def test_fixture_stops_at_verified_without_a_recording_gate(
    tmp_path: Path,
) -> None:
    task, plan, workspace = load_benchmark(tmp_path)
    pipeline, sink = build_pipeline(tmp_path, plan)

    result = pipeline.run(task)

    assert result.final_state is TaskState.VERIFIED
    assert result.execution_report is not None and result.execution_report.success
    assert result.verification_report is not None
    assert result.verification_report.verdict is VerificationVerdict.PASSED
    assert "timeout = 30" in (workspace / "config.toml").read_text(encoding="utf-8")
    event_types = [event.event_type for event in sink.events]
    assert "verification.completed" in event_types
    verified_transition = next(
        event
        for event in sink.events
        if event.event_type == "orchestrator.transition" and event.payload["to_state"] == "verified"
    )
    verification_event = next(
        event for event in sink.events if event.event_type == "verification.completed"
    )
    assert sink.events.index(verification_event) < sink.events.index(verified_transition)
    assert not any(
        event.event_type == "orchestrator.transition"
        and event.payload["to_state"] in {"recorded", "completed"}
        for event in sink.events
    )


def test_wrong_output_never_reaches_verified_state(tmp_path: Path) -> None:
    task, _plan, workspace = load_benchmark(tmp_path)
    wrong_plan = ExecutionPlan(
        task_id=task.task_id,
        planner_kind=PlannerKind.FAKE,
        actions=(
            Action(
                capability="patch_toml",
                arguments={"path": "config.toml", "key": "service.timeout", "value": 31},
            ),
        ),
    )
    pipeline, sink = build_pipeline(tmp_path, wrong_plan)

    result = pipeline.run(task)

    assert result.final_state is TaskState.FAILED
    assert result.execution_report is not None and result.execution_report.success
    assert result.verification_report is not None
    assert result.verification_report.verdict is VerificationVerdict.FAILED
    assert "timeout = 31" in (workspace / "config.toml").read_text(encoding="utf-8")
    assert not any(
        event.event_type == "orchestrator.transition" and event.payload["to_state"] == "verified"
        for event in sink.events
    )


def test_missing_verifier_produces_inconclusive_terminal_failure(tmp_path: Path) -> None:
    task, plan, _workspace = load_benchmark(tmp_path)
    unsupported = task.model_copy(
        update={
            "postconditions": (
                Postcondition(
                    type="unsupported_check",
                    parameters={"path": "config.toml"},
                ),
            )
        }
    )
    pipeline, _sink = build_pipeline(tmp_path, plan)

    result = pipeline.run(unsupported)

    assert result.final_state is TaskState.FAILED
    assert result.verification_report is not None
    assert result.verification_report.verdict is VerificationVerdict.INCONCLUSIVE


def test_capability_error_rolls_back_and_reaches_failed_terminal_state(tmp_path: Path) -> None:
    task, _plan, workspace = load_benchmark(tmp_path)
    original = (workspace / "config.toml").read_text(encoding="utf-8")
    failing_plan = ExecutionPlan(
        task_id=task.task_id,
        planner_kind=PlannerKind.FAKE,
        actions=(
            Action(
                capability="patch_toml",
                arguments={"path": "config.toml", "key": "service.missing", "value": 30},
            ),
        ),
    )
    pipeline, _sink = build_pipeline(tmp_path, failing_plan)

    result = pipeline.run(task)

    assert result.final_state is TaskState.FAILED
    assert result.execution_report is not None
    assert not result.execution_report.success
    assert result.execution_report.rolled_back
    assert result.verification_report is None
    assert (workspace / "config.toml").read_text(encoding="utf-8") == original


def test_incompatible_capability_fails_before_execution(tmp_path: Path) -> None:
    task = TaskSpec.model_validate_json(
        Path("benchmarks/tasks/incompatible/001.json").read_text(encoding="utf-8")
    )
    plan = ExecutionPlan.model_validate_json(
        Path("benchmarks/plans/incompatible/001.json").read_text(encoding="utf-8")
    )
    pipeline, sink = build_pipeline(tmp_path, plan)

    result = pipeline.run(task)

    assert result.final_state is TaskState.FAILED
    assert result.execution_report is None
    assert result.verification_report is None
    assert sink.events[-2].event_type == "authorization.failed"
    assert sink.events[-2].payload["reason"] == "capability is not registered: unknown_capability"
    assert json.loads(sink.events[-1].model_dump_json())["payload"]["to_state"] == "failed"
