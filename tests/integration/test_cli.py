import json
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from typer.testing import CliRunner

from llmin.cli import app
from llmin.domain import Action, ExecutionPlan, PlannerKind, TaskSpec
from llmin.memory import SQLiteMemoryStore
from llmin.planning import OpenRouterPlanner

runner = CliRunner()


def test_validate_task_accepts_benchmark_fixture() -> None:
    fixture = Path("benchmarks/tasks/config_patch/001.json")

    result = runner.invoke(app, ["validate-task", str(fixture)])

    assert result.exit_code == 0
    assert "valid task_id=" in result.stdout
    assert "family=config_patch" in result.stdout


def test_validate_task_rejects_unknown_fields(tmp_path) -> None:
    fixture = tmp_path / "invalid.json"
    fixture.write_text('{"unexpected": true}', encoding="utf-8")

    result = runner.invoke(app, ["validate-task", str(fixture)])

    assert result.exit_code == 2
    assert "invalid:" in result.stderr


def test_validate_task_rejects_empty_request(tmp_path: Path) -> None:
    fixture = tmp_path / "empty.json"
    fixture.write_text("", encoding="utf-8")

    result = runner.invoke(app, ["validate-task", str(fixture)])

    assert result.exit_code == 2
    assert "invalid:" in result.stderr


def test_validate_task_rejects_malformed_json(tmp_path: Path) -> None:
    fixture = tmp_path / "malformed.json"
    fixture.write_text("{", encoding="utf-8")

    result = runner.invoke(app, ["validate-task", str(fixture)])

    assert result.exit_code == 2
    assert "invalid:" in result.stderr


def test_validate_task_rejects_missing_required_parameter(tmp_path: Path) -> None:
    fixture = tmp_path / "missing-objective.json"
    fixture.write_text(
        '{"family":"config_patch","workspace":"sandbox/task","postconditions":[]}',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["validate-task", str(fixture)])

    assert result.exit_code == 2
    assert "objective" in result.stderr


def test_run_fixture_reaches_verified_terminal_state(tmp_path: Path) -> None:
    task_file = Path("benchmarks/tasks/config_patch/001.json")
    plan_file = Path("benchmarks/plans/config_patch/001.json")
    workspace = tmp_path / "sandbox" / "config-patch-001"
    workspace.parent.mkdir()
    shutil.copytree(Path("benchmarks/workspaces/config-patch-001"), workspace)

    result = runner.invoke(
        app,
        ["run-fixture", str(task_file), str(plan_file), str(tmp_path)],
    )

    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    UUID(summary["attempt_id"])
    UUID(summary["trace_id"])
    assert summary["final_state"] == "completed"
    assert summary["execution_success"] is True
    assert summary["verification_verdict"] == "passed"
    assert summary["trace_events"] > 0

    persisted = runner.invoke(
        app,
        ["show-attempt", str(tmp_path / ".llmin" / "memory.sqlite3"), summary["attempt_id"]],
    )

    assert persisted.exit_code == 0
    persisted_summary = json.loads(persisted.stdout)
    assert persisted_summary["attempt_id"] == summary["attempt_id"]
    assert persisted_summary["trace_id"] == summary["trace_id"]
    assert persisted_summary["status"] == "finalized"
    assert persisted_summary["final_state"] == "completed"
    assert persisted_summary["planner_kind"] == "fake"
    assert persisted_summary["execution_success"] is True
    assert persisted_summary["verification_verdict"] == "passed"
    assert persisted_summary["diagnostics"] == []
    assert persisted_summary["state_sequence"] == [
        "received",
        "routed",
        "planned",
        "authorized",
        "executed",
        "verified",
        "recorded",
        "completed",
    ]
    assert persisted_summary["trace_events"] == summary["trace_events"]
    assert len(persisted_summary["evidence"]) == 1


def test_show_attempt_reports_unknown_id(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    SQLiteMemoryStore(database)

    result = runner.invoke(app, ["show-attempt", str(database), str(uuid4())])

    assert result.exit_code == 1
    assert "attempt not found:" in result.stderr


def test_run_agent_uses_llm_plan_then_executes_verifies_and_persists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_file = Path("benchmarks/tasks/config_patch/001.json")
    task = TaskSpec.model_validate_json(task_file.read_text(encoding="utf-8"))
    workspace = tmp_path / task.workspace
    workspace.parent.mkdir(parents=True)
    shutil.copytree(Path("benchmarks/workspaces/config-patch-001"), workspace)
    plan = ExecutionPlan(
        task_id=task.task_id,
        planner_kind=PlannerKind.LLM,
        actions=(
            Action(
                capability="patch_toml",
                arguments={"path": "config.toml", "key": "service.timeout", "value": 30},
            ),
        ),
        estimated_cost_usd="0.001",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")
    monkeypatch.setattr(OpenRouterPlanner, "plan", lambda self, _task: plan)

    result = runner.invoke(
        app,
        ["run-agent", str(task_file), str(tmp_path), "--model", "test/model"],
    )

    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["final_state"] == "completed"
    assert summary["planner_kind"] == "llm"
    assert summary["estimated_cost_usd"] == "0.001"
    assert summary["execution_success"] is True
    assert summary["verification_verdict"] == "passed"
    attempt = SQLiteMemoryStore(tmp_path / ".llmin" / "memory.sqlite3").get_attempt(
        UUID(summary["attempt_id"])
    )
    assert attempt is not None
    assert attempt.plan is not None and attempt.plan.planner_kind is PlannerKind.LLM
    assert "timeout = 30" in (workspace / "config.toml").read_text(encoding="utf-8")
    persisted = runner.invoke(
        app,
        ["show-attempt", str(tmp_path / ".llmin" / "memory.sqlite3"), summary["attempt_id"]],
    )
    persisted_summary = json.loads(persisted.stdout)
    assert persisted_summary["planner_provider"] == "openrouter"
    assert persisted_summary["planner_model"] == "test/model"


def test_benchmark_command_writes_passing_report(tmp_path: Path) -> None:
    output = tmp_path / "report.json"

    result = runner.invoke(
        app,
        [
            "benchmark",
            "benchmarks/stage1-suite.json",
            "--seed",
            "11",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert summary["quality_gate_passed"] is True
    assert summary["unsafe_acceptances"] == 0
    assert report["metrics"]["total_cases"] == 17


def test_benchmark_command_writes_report_when_quality_gate_fails(tmp_path: Path) -> None:
    manifest = json.loads(Path("benchmarks/stage1-suite.json").read_text(encoding="utf-8"))
    manifest["cases"][0]["expected"]["final_state"] = "failed"
    manifest["cases"][0]["expected"]["verification_verdict"] = "failed"
    suite = tmp_path / "failing-suite.json"
    suite.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "failed-report.json"

    result = runner.invoke(
        app,
        ["benchmark", str(suite), "--seed", "0", "--output", str(output)],
    )

    assert result.exit_code == 1
    summary = json.loads(result.stdout)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert summary["quality_gate_passed"] is False
    assert report["metrics"]["matched_cases"] == 16
    assert report["metrics"]["quality_gate_passed"] is False
