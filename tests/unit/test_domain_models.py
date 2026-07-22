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
        "postconditions": (
            Postcondition(type="toml_value_equals", parameters={"value": 30}),
        ),
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
