import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from llmin.domain import (
    Action,
    ExecutionPlan,
    PlannerKind,
    Postcondition,
    TaskSpec,
    VerificationVerdict,
)
from llmin.execution import CapabilityRegistry, Executor, SandboxFactory
from llmin.knowledge import (
    CompiledSkill,
    ExactMatchKnowledgeRouter,
    RouteOutcome,
    RoutingPlanner,
    task_signature,
)
from llmin.memory import ActivationState, Applicability, Provenance, RetentionPolicy
from llmin.observability import InMemoryTraceSink
from llmin.orchestrator import TaskState
from llmin.pipeline import Pipeline
from llmin.planning import FakePlanner, Planner
from llmin.verification import VerificationService, VerifierRegistry

ENVIRONMENT = "d" * 64


def build_pipeline(
    base_root: Path,
    plan: ExecutionPlan,
    verifier_registry: VerifierRegistry | None = None,
) -> tuple[Pipeline, InMemoryTraceSink]:
    return build_pipeline_with_planner(
        base_root,
        FakePlanner(lambda _task: plan),
        verifier_registry=verifier_registry,
    )


def build_pipeline_with_planner(
    base_root: Path,
    planner: Planner,
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
            planner=planner,
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


def make_skill(
    task: TaskSpec,
    plan: ExecutionPlan,
    *,
    required_capabilities: frozenset[str] | None = None,
) -> CompiledSkill:
    skill_id = uuid4()
    report_id = uuid4()
    compiled_plan = plan.model_copy(
        update={
            "task_id": task.task_id,
            "planner_kind": PlannerKind.COMPILED,
            "knowledge_artifact_id": skill_id,
        }
    )
    return CompiledSkill(
        artifact_id=skill_id,
        content_hash="e" * 64,
        provenance=Provenance(
            source_event_ids=frozenset({uuid4()}),
            evidence_ids=frozenset({uuid4()}),
            verification_report_ids=frozenset({report_id}),
        ),
        applicability=Applicability(
            family=task.family,
            environment_fingerprints=frozenset({ENVIRONMENT}),
            required_capabilities=(
                required_capabilities
                if required_capabilities is not None
                else frozenset(action.capability for action in plan.actions)
            ),
        ),
        retention=RetentionPolicy(
            minimum_retain_until=datetime.now(UTC) + timedelta(days=30),
            reason="verified integration fixture",
        ),
        activation_state=ActivationState.ACTIVE,
        task_signature=task_signature(task),
        plan=compiled_plan,
        source_task_id=task.task_id,
        source_attempt_id=uuid4(),
        source_verification_report_id=report_id,
    )


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


def test_compiled_skill_runs_through_sandbox_and_verifier_without_fallback(
    tmp_path: Path,
) -> None:
    source_task, source_plan, workspace = load_benchmark(tmp_path)
    repeated_task = source_task.model_copy(update={"task_id": uuid4()})
    fallback_calls = 0

    def fallback_factory(task: TaskSpec) -> ExecutionPlan:
        nonlocal fallback_calls
        fallback_calls += 1
        return source_plan.model_copy(update={"task_id": task.task_id})

    planner = RoutingPlanner(
        router=ExactMatchKnowledgeRouter(),
        fallback=FakePlanner(fallback_factory),
        skill_provider=lambda: (make_skill(source_task, source_plan),),
        environment_fingerprint=lambda task: ENVIRONMENT,
    )
    pipeline, _sink = build_pipeline_with_planner(tmp_path, planner)

    result = pipeline.run(repeated_task)

    assert result.final_state is TaskState.VERIFIED
    assert result.execution_plan is not None
    assert result.execution_plan.planner_kind is PlannerKind.COMPILED
    assert result.execution_report is not None and result.execution_report.success
    assert result.verification_report is not None
    assert result.verification_report.verdict is VerificationVerdict.PASSED
    assert fallback_calls == 0
    assert planner.last_decision is not None
    assert planner.last_decision.outcome is RouteOutcome.HIT
    assert planner.last_decision.llm_calls == 0
    assert "timeout = 30" in (workspace / "config.toml").read_text(encoding="utf-8")


def test_semantic_mutation_uses_fallback_and_still_reaches_verifier(tmp_path: Path) -> None:
    source_task, source_plan, workspace = load_benchmark(tmp_path)
    mutated_task = source_task.model_copy(
        update={"task_id": uuid4(), "objective": "Set the service timeout safely"}
    )
    fallback_calls = 0

    def fallback_factory(task: TaskSpec) -> ExecutionPlan:
        nonlocal fallback_calls
        fallback_calls += 1
        return source_plan.model_copy(
            update={"task_id": task.task_id, "planner_kind": PlannerKind.LLM}
        )

    planner = RoutingPlanner(
        router=ExactMatchKnowledgeRouter(),
        fallback=FakePlanner(fallback_factory),
        skill_provider=lambda: (make_skill(source_task, source_plan),),
        environment_fingerprint=lambda task: ENVIRONMENT,
    )
    pipeline, _sink = build_pipeline_with_planner(tmp_path, planner)

    result = pipeline.run(mutated_task)

    assert result.final_state is TaskState.VERIFIED
    assert result.execution_plan is not None
    assert result.execution_plan.planner_kind is PlannerKind.LLM
    assert fallback_calls == 1
    assert planner.last_decision is not None
    assert planner.last_decision.outcome is RouteOutcome.REJECTED
    assert planner.last_decision.llm_calls == 1
    assert "timeout = 30" in (workspace / "config.toml").read_text(encoding="utf-8")


def test_compiled_skill_cannot_bypass_capability_authorization(tmp_path: Path) -> None:
    task, source_plan, workspace = load_benchmark(tmp_path)
    original = (workspace / "config.toml").read_text(encoding="utf-8")
    unsafe_plan = source_plan.model_copy(
        update={
            "actions": (
                Action(capability="unknown_capability", arguments={}),
            )
        }
    )
    planner = RoutingPlanner(
        router=ExactMatchKnowledgeRouter(),
        fallback=FakePlanner(lambda current: source_plan.model_copy(update={"task_id": current.task_id})),
        skill_provider=lambda: (
            make_skill(task, unsafe_plan, required_capabilities=frozenset()),
        ),
        environment_fingerprint=lambda current: ENVIRONMENT,
    )
    pipeline, sink = build_pipeline_with_planner(tmp_path, planner)

    result = pipeline.run(task)

    assert planner.last_decision is not None
    assert planner.last_decision.outcome is RouteOutcome.HIT
    assert result.final_state is TaskState.FAILED
    assert result.execution_report is None
    assert result.verification_report is None
    assert (workspace / "config.toml").read_text(encoding="utf-8") == original
    assert any(event.event_type == "authorization.failed" for event in sink.events)


def test_wrong_compiled_skill_is_rejected_by_independent_verifier(tmp_path: Path) -> None:
    task, source_plan, workspace = load_benchmark(tmp_path)
    wrong_plan = source_plan.model_copy(
        update={
            "actions": (
                Action(
                    capability="patch_toml",
                    arguments={
                        "path": "config.toml",
                        "key": "service.timeout",
                        "value": 31,
                    },
                ),
            )
        }
    )
    planner = RoutingPlanner(
        router=ExactMatchKnowledgeRouter(),
        fallback=FakePlanner(lambda current: source_plan.model_copy(update={"task_id": current.task_id})),
        skill_provider=lambda: (make_skill(task, wrong_plan),),
        environment_fingerprint=lambda current: ENVIRONMENT,
    )
    pipeline, _sink = build_pipeline_with_planner(tmp_path, planner)

    result = pipeline.run(task)

    assert planner.last_decision is not None
    assert planner.last_decision.outcome is RouteOutcome.HIT
    assert result.final_state is TaskState.FAILED
    assert result.execution_report is not None and result.execution_report.success
    assert result.verification_report is not None
    assert result.verification_report.verdict is VerificationVerdict.FAILED
    assert "timeout = 31" in (workspace / "config.toml").read_text(encoding="utf-8")
