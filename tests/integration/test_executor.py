from pathlib import Path

from llmin.domain import (
    Action,
    Budget,
    ExecutionPlan,
    PlannerKind,
    Postcondition,
    TaskConstraints,
    TaskSpec,
)
from llmin.execution import CapabilityRegistry, Executor, Sandbox


def make_task(
    *,
    capabilities: frozenset[str] = frozenset({"read_text", "write_text_atomic"}),
    writable_paths: tuple[str, ...] = ("target.txt",),
    max_actions: int = 5,
    timeout_seconds: int = 30,
) -> TaskSpec:
    return TaskSpec(
        family="text_update",
        objective="Update a text fixture",
        workspace="sandbox/executor-test",
        constraints=TaskConstraints(
            writable_paths=writable_paths,
            allowed_capabilities=capabilities,
        ),
        postconditions=(Postcondition(type="text_equals", parameters={"value": "after"}),),
        budget=Budget(max_actions=max_actions, timeout_seconds=timeout_seconds),
    )


def make_plan(task: TaskSpec, *actions: Action) -> ExecutionPlan:
    return ExecutionPlan(
        task_id=task.task_id,
        planner_kind=PlannerKind.FAKE,
        actions=actions,
    )


def test_executor_commits_authorized_atomic_write(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    task = make_task()
    plan = make_plan(
        task,
        Action(
            capability="write_text_atomic",
            arguments={"path": "target.txt", "content": "after"},
        ),
        Action(capability="read_text", arguments={"path": "target.txt"}),
    )
    executor = Executor(CapabilityRegistry.with_builtins())

    report = executor.execute(
        task=task,
        plan=plan,
        sandbox=Sandbox(tmp_path, writable_paths=task.constraints.writable_paths),
    )

    assert report.success
    assert target.read_text(encoding="utf-8") == "after"
    assert report.action_results[-1].output["content"] == "after"
    assert report.changes[0].path == "target.txt"


def test_executor_rolls_back_earlier_write_when_later_action_fails(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    task = make_task(writable_paths=("target.txt", "missing-parent/other.txt"))
    plan = make_plan(
        task,
        Action(
            capability="write_text_atomic",
            arguments={"path": "target.txt", "content": "after"},
        ),
        Action(
            capability="write_text_atomic",
            arguments={"path": "missing-parent/other.txt", "content": "fail"},
        ),
    )
    executor = Executor(CapabilityRegistry.with_builtins())

    report = executor.execute(
        task=task,
        plan=plan,
        sandbox=Sandbox(tmp_path, writable_paths=task.constraints.writable_paths),
    )

    assert not report.success
    assert report.rolled_back
    assert target.read_text(encoding="utf-8") == "before"
    assert report.action_results[-1].success is False


def test_executor_rejects_capability_not_authorized_by_task(tmp_path: Path) -> None:
    task = make_task(capabilities=frozenset({"read_text"}))
    plan = make_plan(
        task,
        Action(
            capability="write_text_atomic",
            arguments={"path": "target.txt", "content": "after"},
        ),
    )
    executor = Executor(CapabilityRegistry.with_builtins())

    report = executor.execute(
        task=task,
        plan=plan,
        sandbox=Sandbox(tmp_path, writable_paths=task.constraints.writable_paths),
    )

    assert not report.success
    assert not report.rolled_back
    assert "not authorized" in report.error


def test_executor_rejects_plan_over_action_budget(tmp_path: Path) -> None:
    task = make_task(max_actions=1)
    action = Action(capability="read_text", arguments={"path": "target.txt"})
    plan = make_plan(task, action, action.model_copy(update={"action_id": action.action_id}))
    executor = Executor(CapabilityRegistry.with_builtins())

    report = executor.execute(
        task=task,
        plan=plan,
        sandbox=Sandbox(tmp_path, writable_paths=task.constraints.writable_paths),
    )

    assert not report.success
    assert report.error == "plan exceeds the action budget"


def test_executor_rejects_mismatched_sandbox_policy(tmp_path: Path) -> None:
    task = make_task()
    plan = make_plan(task, Action(capability="read_text", arguments={"path": "target.txt"}))
    executor = Executor(CapabilityRegistry.with_builtins())

    report = executor.execute(task=task, plan=plan, sandbox=Sandbox(tmp_path))

    assert not report.success
    assert "does not match TaskSpec" in report.error


def test_executor_rolls_back_when_elapsed_budget_is_exceeded(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    task = make_task(timeout_seconds=1)
    plan = make_plan(
        task,
        Action(
            capability="write_text_atomic",
            arguments={"path": "target.txt", "content": "after"},
        ),
    )
    timestamps = iter((0.0, 0.0, 2.0))
    executor = Executor(CapabilityRegistry.with_builtins(), clock=lambda: next(timestamps))

    report = executor.execute(
        task=task,
        plan=plan,
        sandbox=Sandbox(tmp_path, writable_paths=task.constraints.writable_paths),
    )

    assert not report.success
    assert report.rolled_back
    assert report.error == "execution timeout exceeded"
    assert target.read_text(encoding="utf-8") == "before"
