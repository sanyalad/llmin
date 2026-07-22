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
