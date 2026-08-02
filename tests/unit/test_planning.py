import json
from decimal import Decimal
from typing import Any

import pytest

from llmin.domain import Budget, PlannerKind, Postcondition, TaskConstraints, TaskSpec
from llmin.planning import OpenRouterPlanner, PlanningError


class StubTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        return self.response


def make_task(*, budget: Budget | None = None) -> TaskSpec:
    return TaskSpec(
        family="config_patch",
        objective="Set the service timeout to 30 seconds",
        workspace="sandbox/task",
        inputs={"files": ["config.toml"]},
        constraints=TaskConstraints(
            readable_paths=("config.toml",),
            writable_paths=("config.toml",),
            allowed_capabilities=frozenset({"patch_toml"}),
        ),
        postconditions=(
            Postcondition(
                type="toml_value_equals",
                parameters={"path": "config.toml", "key": "service.timeout", "value": 30},
            ),
        ),
        budget=budget or Budget(max_llm_calls=1, max_cost_usd="0.01", max_actions=2),
    )


def completion(actions: list[dict[str, Any]], *, cost: object = "0.002") -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": json.dumps({"actions": actions})},
            }
        ],
        "usage": {"cost": cost},
    }


def test_openrouter_planner_builds_local_plan_from_strict_actions() -> None:
    task = make_task()
    transport = StubTransport(
        completion(
            [
                {
                    "capability": "patch_toml",
                    "arguments": {
                        "path": "config.toml",
                        "key": "service.timeout",
                        "value": 30,
                    },
                }
            ]
        )
    )

    plan = OpenRouterPlanner(model="test/model", transport=transport).plan(task)

    assert plan.task_id == task.task_id
    assert plan.planner_kind is PlannerKind.LLM
    assert plan.estimated_cost_usd == Decimal("0.002")
    assert plan.actions[0].capability == "patch_toml"
    request = transport.requests[0]
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["provider"] == {"require_parameters": True}
    assert request["stream"] is False


def test_openrouter_planner_rejects_unauthorized_returned_capability() -> None:
    transport = StubTransport(
        completion(
            [
                {
                    "capability": "write_text_atomic",
                    "arguments": {"path": "config.toml", "content": "unsafe"},
                }
            ]
        )
    )

    with pytest.raises(PlanningError, match="unauthorized capability"):
        OpenRouterPlanner(model="test/model", transport=transport).plan(make_task())


def test_openrouter_planner_refuses_zero_call_budget_without_network() -> None:
    transport = StubTransport(completion([]))
    task = make_task(budget=Budget(max_llm_calls=0, max_cost_usd="0.01", max_actions=2))

    with pytest.raises(PlanningError, match="does not allow an LLM call"):
        OpenRouterPlanner(model="test/model", transport=transport).plan(task)

    assert transport.requests == []


def test_openrouter_planner_rejects_reported_cost_over_budget() -> None:
    transport = StubTransport(
        completion(
            [
                {
                    "capability": "patch_toml",
                    "arguments": {
                        "path": "config.toml",
                        "key": "service.timeout",
                        "value": 30,
                    },
                }
            ],
            cost="0.02",
        )
    )

    with pytest.raises(PlanningError, match="exceeded the task cost budget"):
        OpenRouterPlanner(model="test/model", transport=transport).plan(make_task())


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"error": {"message": "provider failed"}},
        {"choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}]},
        completion([]),
    ],
)
def test_openrouter_planner_rejects_invalid_provider_responses(
    response: dict[str, Any],
) -> None:
    with pytest.raises(PlanningError):
        OpenRouterPlanner(model="test/model", transport=StubTransport(response)).plan(make_task())
