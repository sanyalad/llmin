from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from llmin.domain import (
    Action,
    Budget,
    ExecutionPlan,
    PlannerKind,
    Postcondition,
    TaskConstraints,
    TaskSpec,
)
from llmin.knowledge import (
    CompiledSkill,
    ExactMatchKnowledgeRouter,
    RouteOutcome,
    task_signature,
)
from llmin.memory import (
    ActivationState,
    Applicability,
    Provenance,
    RetentionPolicy,
)

ENVIRONMENT = "a" * 64


def make_task(*, objective: str = "write configuration", max_llm_calls: int = 1) -> TaskSpec:
    return TaskSpec(
        family="config_patch",
        objective=objective,
        workspace="workspace",
        inputs={"path": "app.json", "value": 3},
        constraints=TaskConstraints(
            readable_paths=("app.json",),
            writable_paths=("app.json",),
            allowed_capabilities=frozenset({"write_file"}),
        ),
        postconditions=(
            Postcondition(
                type="file_contains",
                parameters={"path": "app.json", "text": "3"},
            ),
        ),
        budget=Budget(max_llm_calls=max_llm_calls),
    )


def make_skill(
    task: TaskSpec,
    *,
    active: bool = True,
    environment: str = ENVIRONMENT,
) -> CompiledSkill:
    skill_id = uuid4()
    report_id = uuid4()
    attempt_id = uuid4()
    plan = ExecutionPlan(
        task_id=task.task_id,
        planner_kind=PlannerKind.COMPILED,
        knowledge_artifact_id=skill_id,
        actions=(
            Action(
                capability="write_file",
                arguments={"path": "app.json", "content": "3"},
            ),
        ),
    )
    return CompiledSkill(
        artifact_id=skill_id,
        content_hash="b" * 64,
        provenance=Provenance(
            source_event_ids=frozenset({uuid4()}),
            evidence_ids=frozenset({uuid4()}),
            verification_report_ids=frozenset({report_id}),
        ),
        applicability=Applicability(
            family=task.family,
            environment_fingerprints=frozenset({environment}),
            required_capabilities=frozenset({"write_file"}),
        ),
        retention=RetentionPolicy(
            minimum_retain_until=datetime.now(UTC) + timedelta(days=30),
            reason="verified compiled skill",
        ),
        activation_state=ActivationState.ACTIVE if active else ActivationState.CANDIDATE,
        task_signature=task_signature(task),
        plan=plan,
        source_task_id=task.task_id,
        source_attempt_id=attempt_id,
        source_verification_report_id=report_id,
    )


def test_task_signature_ignores_identity_and_budget() -> None:
    first = make_task(max_llm_calls=1)
    second = make_task(max_llm_calls=9)

    assert first.task_id != second.task_id
    assert task_signature(first) == task_signature(second)


def test_task_signature_changes_with_semantics() -> None:
    assert task_signature(make_task()) != task_signature(
        make_task(objective="delete configuration")
    )


def test_active_exact_match_returns_compiled_plan_without_llm_call() -> None:
    source = make_task()
    repeated = make_task()
    skill = make_skill(source)

    decision = ExactMatchKnowledgeRouter().route(
        task=repeated,
        environment_fingerprint=ENVIRONMENT,
        skills=(skill,),
    )

    assert decision.outcome is RouteOutcome.HIT
    assert decision.llm_calls == 0
    assert decision.skill_id == skill.artifact_id
    assert decision.plan is not None
    assert decision.plan.task_id == repeated.task_id
    assert decision.plan.planner_kind is PlannerKind.COMPILED


def test_missing_family_routes_to_fallback() -> None:
    task = make_task()

    decision = ExactMatchKnowledgeRouter().route(
        task=task,
        environment_fingerprint=ENVIRONMENT,
        skills=(),
    )

    assert decision.outcome is RouteOutcome.MISS
    assert decision.llm_calls == 1
    assert decision.plan is None


def test_inactive_skill_is_rejected() -> None:
    task = make_task()

    decision = ExactMatchKnowledgeRouter().route(
        task=task,
        environment_fingerprint=ENVIRONMENT,
        skills=(make_skill(task, active=False),),
    )

    assert decision.outcome is RouteOutcome.REJECTED
    assert decision.llm_calls == 1


def test_environment_mismatch_is_rejected() -> None:
    task = make_task()

    decision = ExactMatchKnowledgeRouter().route(
        task=task,
        environment_fingerprint="c" * 64,
        skills=(make_skill(task),),
    )

    assert decision.outcome is RouteOutcome.REJECTED
    assert decision.llm_calls == 1


def test_semantic_mutation_is_rejected() -> None:
    source = make_task()
    mutation = make_task(objective="delete configuration")

    decision = ExactMatchKnowledgeRouter().route(
        task=mutation,
        environment_fingerprint=ENVIRONMENT,
        skills=(make_skill(source),),
    )

    assert decision.outcome is RouteOutcome.REJECTED
    assert decision.llm_calls == 1
