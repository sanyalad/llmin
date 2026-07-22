"""Command-line entrypoint for inspecting and validating Stage 1 artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from llmin import __version__
from llmin.domain import ExecutionPlan, TaskSpec
from llmin.execution import CapabilityRegistry, Executor, SandboxFactory
from llmin.observability import InMemoryTraceSink
from llmin.orchestrator import TaskState
from llmin.pipeline import Pipeline
from llmin.planning import FakePlanner
from llmin.verification import VerificationService, VerifierRegistry

app = typer.Typer(no_args_is_help=True, help="LLMIN Stage 1 tools")


@app.command()
def version() -> None:
    """Print the installed LLMIN version."""

    typer.echo(__version__)


@app.command("validate-task")
def validate_task(
    task_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
) -> None:
    """Validate a JSON TaskSpec without executing it."""

    try:
        payload = json.loads(task_file.read_text(encoding="utf-8"))
        task = TaskSpec.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        typer.echo(f"invalid: {error}", err=True)
        raise typer.Exit(code=2) from error

    typer.echo(f"valid task_id={task.task_id} family={task.family}")


@app.command("run-fixture")
def run_fixture(
    task_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    plan_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    base_root: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True, readable=True),
    ],
) -> None:
    """Run a deterministic fixture through execution and independent verification."""

    try:
        task = TaskSpec.model_validate_json(task_file.read_text(encoding="utf-8"))
        plan = ExecutionPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as error:
        typer.echo(f"invalid fixture: {error}", err=True)
        raise typer.Exit(code=2) from error

    sink = InMemoryTraceSink()
    sandbox_factory = SandboxFactory(base_root)
    executor = Executor(
        CapabilityRegistry.with_builtins(),
        sandbox_factory=sandbox_factory,
        trace_sink=sink,
    )
    verification = VerificationService(
        VerifierRegistry.with_builtins(),
        sandbox_factory=sandbox_factory,
        trace_sink=sink,
    )
    pipeline = Pipeline(
        planner=FakePlanner(lambda _task: plan),
        executor=executor,
        verification=verification,
        trace_sink=sink,
    )
    result = pipeline.run(task)
    summary = {
        "task_id": str(task.task_id),
        "final_state": result.final_state.value,
        "execution_success": (
            result.execution_report.success if result.execution_report is not None else None
        ),
        "verification_verdict": (
            result.verification_report.verdict.value
            if result.verification_report is not None
            else None
        ),
        "trace_events": len(sink.events),
    }
    typer.echo(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if result.final_state is not TaskState.COMPLETED:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
