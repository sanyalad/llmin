from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from llmin.domain import (
    Action,
    Budget,
    ExecutionPlan,
    PlannerKind,
    Postcondition,
    TaskConstraints,
    TaskSpec,
    VerificationReport,
    VerificationVerdict,
)


def make_task(**overrides: object) -> TaskSpec:
    values: dict[str, object] = {
        "family": "config_patch",
        "objective": "Set timeout",
        "workspace": "sandbox/task-1",
        "constraints": TaskConstraints(
            writable_paths=("config.toml",),
            allowed_capabilities=frozenset({"patch_toml"}),
        ),
        "postconditions": (Postcondition(type="toml_value_equals", parameters={"value": 30}),),
    }
    values.update(overrides)
    return TaskSpec.model_validate(values)


@pytest.mark.parametrize(
    "path",
    [
        ".",
        "../outside",
        "sandbox/../outside",
        "sandbox//task",
        "sandbox/./task",
        "/absolute",
        "C:/absolute",
        "a\\b",
    ],
)
def test_task_rejects_paths_that_can_escape_or_vary_by_platform(path: str) -> None:
    with pytest.raises(ValidationError):
        make_task(workspace=path)


def test_task_requires_timezone_aware_creation_time() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_task(created_at=datetime(2026, 7, 22))


def test_budget_uses_exact_decimal_cost() -> None:
    budget = Budget(max_cost_usd="0.015")

    assert budget.max_cost_usd == Decimal("0.015")


def test_known_knowledge_plan_requires_artifact_id() -> None:
    with pytest.raises(ValidationError, match="knowledge_artifact_id"):
        ExecutionPlan(
            task_id=uuid4(),
            planner_kind=PlannerKind.COMPILED,
            actions=(Action(capability="patch_toml"),),
        )


def test_llm_plan_cannot_claim_knowledge_artifact() -> None:
    with pytest.raises(ValidationError, match="cannot claim"):
        ExecutionPlan(
            task_id=uuid4(),
            planner_kind=PlannerKind.LLM,
            knowledge_artifact_id=uuid4(),
            actions=(Action(capability="patch_toml"),),
        )


def test_contract_rejects_non_json_values_before_serialization() -> None:
    with pytest.raises(ValidationError, match="not JSON-compatible"):
        Action(capability="read_text", arguments={"opaque": object()})


def test_nested_json_contracts_are_immutable_and_serializable() -> None:
    action = Action(
        capability="read_text",
        arguments={"nested": {"items": [1, 2, 3]}},
    )

    with pytest.raises(TypeError, match="cannot be mutated"):
        action.arguments["new"] = "value"
    with pytest.raises(TypeError, match="cannot be mutated"):
        action.arguments["nested"]["new"] = "value"
    assert action.model_dump_json()


def test_plan_rejects_duplicate_action_ids() -> None:
    action = Action(capability="read_text", arguments={"path": "input.txt"})

    with pytest.raises(ValidationError, match="action_id values must be unique"):
        ExecutionPlan(
            task_id=uuid4(),
            planner_kind=PlannerKind.FAKE,
            actions=(action, action),
        )


def test_contract_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValidationError, match=r"Input should be '1\.0'"):
        make_task(schema_version="2.0")


def test_removed_timeout_fields_cannot_create_false_guarantees() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Action(capability="read_text", timeout_seconds=1)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Budget(timeout_seconds=1)


def test_passed_verification_requires_evidence_and_full_coverage() -> None:
    with pytest.raises(ValidationError, match="cover all required postconditions"):
        VerificationReport(
            task_id=uuid4(),
            attempt_id=uuid4(),
            verdict=VerificationVerdict.PASSED,
            required_postconditions=frozenset({0}),
            covered_postconditions=frozenset(),
            verifier_version="test",
        )
