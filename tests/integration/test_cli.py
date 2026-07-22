import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from llmin.cli import app

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
    assert summary["final_state"] == "completed"
    assert summary["execution_success"] is True
    assert summary["verification_verdict"] == "passed"
    assert summary["trace_events"] > 0


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
    assert report["metrics"]["total_cases"] == 13
