from pathlib import Path
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
from llmin.execution import CapabilityRegistry, Executor, SandboxFactory
from llmin.execution.sandbox import SandboxTransaction
from llmin.observability import InMemoryTraceSink


def make_task(
    *,
    workspace: str = "workspace",
    capabilities: frozenset[str] = frozenset({"read_text", "write_text_atomic"}),
    readable_paths: tuple[str, ...] = ("target.txt",),
    writable_paths: tuple[str, ...] = ("target.txt",),
    max_actions: int = 5,
) -> TaskSpec:
    return TaskSpec(
        family="text_update",
        objective="Update a text fixture",
        workspace=workspace,
        constraints=TaskConstraints(
            readable_paths=readable_paths,
            writable_paths=writable_paths,
            allowed_capabilities=capabilities,
        ),
        postconditions=(
            Postcondition(
                type="text_equals",
                parameters={"path": "target.txt", "value": "after"},
            ),
        ),
        budget=Budget(max_actions=max_actions),
    )


def make_plan(task: TaskSpec, *actions: Action) -> ExecutionPlan:
    return ExecutionPlan(
        task_id=task.task_id,
        planner_kind=PlannerKind.FAKE,
        actions=actions,
    )


def make_executor(
    base_root: Path,
    registry: CapabilityRegistry | None = None,
) -> tuple[Executor, InMemoryTraceSink]:
    sink = InMemoryTraceSink()
    executor = Executor(
        registry or CapabilityRegistry.with_builtins(),
        sandbox_factory=SandboxFactory(base_root),
        trace_sink=sink,
    )
    return executor, sink


def execute(executor: Executor, task: TaskSpec, plan: ExecutionPlan):
    return executor.execute(
        task=task,
        plan=plan,
        trace_id=uuid4(),
        attempt_id=uuid4(),
    )


def test_executor_commits_authorized_atomic_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
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
    executor, sink = make_executor(tmp_path)

    report = execute(executor, task, plan)

    assert report.success
    assert target.read_text(encoding="utf-8") == "after"
    assert report.action_results[-1].output["characters"] == len("after")
    assert "content" not in report.action_results[-1].output
    assert report.changes[0].path == "target.txt"
    assert [event.event_type for event in sink.events] == [
        "execution.preflight_authorized",
        "execution.action_started",
        "execution.action_completed",
        "execution.action_started",
        "execution.action_completed",
        "execution.completed",
    ]


def test_executor_rolls_back_earlier_write_when_later_action_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
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
    executor, sink = make_executor(tmp_path)

    report = execute(executor, task, plan)

    assert not report.success
    assert report.rolled_back
    assert target.read_text(encoding="utf-8") == "before"
    assert report.action_results[-1].success is False
    assert sink.events[-1].event_type == "execution.rollback_completed"


class _ExplodingCapability:
    name = "explode_after_write"

    def execute(
        self,
        arguments: dict[str, object],
        transaction: SandboxTransaction,
    ) -> dict[str, object]:
        transaction.write_text_atomic(str(arguments["path"]), "changed")
        raise RuntimeError("internal detail must not escape")


def test_unexpected_capability_error_is_reported_and_rolled_back(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    registry = CapabilityRegistry.with_builtins()
    registry.register(_ExplodingCapability())
    task = make_task(capabilities=frozenset({"explode_after_write"}))
    plan = make_plan(
        task,
        Action(capability="explode_after_write", arguments={"path": "target.txt"}),
    )
    executor, _sink = make_executor(tmp_path, registry)

    report = execute(executor, task, plan)

    assert not report.success
    assert report.rolled_back
    assert report.error == "unexpected capability failure"
    assert report.error_type == "RuntimeError"
    assert target.read_text(encoding="utf-8") == "before"


def test_executor_uses_only_declared_workspace(tmp_path: Path) -> None:
    declared = tmp_path / "declared" / "root"
    declared.mkdir(parents=True)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (declared / "target.txt").write_text("before", encoding="utf-8")
    (decoy / "target.txt").write_text("decoy", encoding="utf-8")
    task = make_task(workspace="declared/root")
    plan = make_plan(
        task,
        Action(
            capability="write_text_atomic",
            arguments={"path": "target.txt", "content": "after"},
        ),
    )
    executor, _sink = make_executor(tmp_path)

    report = execute(executor, task, plan)

    assert report.success
    assert (declared / "target.txt").read_text(encoding="utf-8") == "after"
    assert (decoy / "target.txt").read_text(encoding="utf-8") == "decoy"


def test_executor_rejects_capability_not_authorized_by_task(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir()
    task = make_task(capabilities=frozenset({"read_text"}))
    plan = make_plan(
        task,
        Action(
            capability="write_text_atomic",
            arguments={"path": "target.txt", "content": "after"},
        ),
    )
    executor, _sink = make_executor(tmp_path)

    report = execute(executor, task, plan)

    assert not report.success
    assert not report.rolled_back
    assert "not authorized" in report.error


def test_executor_rejects_plan_over_action_budget(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir()
    task = make_task(max_actions=1)
    plan = make_plan(
        task,
        Action(capability="read_text", arguments={"path": "target.txt"}),
        Action(capability="read_text", arguments={"path": "target.txt"}),
    )
    executor, _sink = make_executor(tmp_path)

    report = execute(executor, task, plan)

    assert not report.success
    assert report.error == "plan exceeds the action budget"


def test_reading_requires_exact_readable_allowlist(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "target.txt").write_text("allowed", encoding="utf-8")
    (workspace / "private.txt").write_text("secret", encoding="utf-8")
    task = make_task(readable_paths=("target.txt",))
    plan = make_plan(task, Action(capability="read_text", arguments={"path": "private.txt"}))
    executor, _sink = make_executor(tmp_path)

    report = execute(executor, task, plan)

    assert not report.success
    assert "read target is not allowlisted" in report.error
