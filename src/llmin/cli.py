"""Command-line entrypoint for inspecting and validating Stage 1 artifacts."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from pydantic import ValidationError

from llmin import __version__
from llmin.benchmark import BenchmarkRunner, BenchmarkSplit, BenchmarkSuite
from llmin.domain import ExecutionPlan, TaskSpec
from llmin.execution import CapabilityRegistry, Executor, SandboxFactory
from llmin.memory import (
    AttemptCoordinator,
    ContentAddressedArtifactStore,
    EnvironmentProbe,
    SQLiteMemoryStore,
)
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

    memory_root = base_root / ".llmin"
    memory_root.mkdir(exist_ok=True)
    sink = SQLiteMemoryStore(memory_root / "memory.sqlite3")
    artifacts = ContentAddressedArtifactStore(memory_root / "artifacts")
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
    coordinated = AttemptCoordinator(memory=sink, artifacts=artifacts).run(
        pipeline=pipeline,
        task=task,
        environment_attributes=EnvironmentProbe().capture(),
    )
    result = coordinated.result
    events = sink.reconstruct_attempt(result.attempt_id).trace_events
    summary = {
        "task_id": str(task.task_id),
        "attempt_id": str(result.attempt_id),
        "trace_id": str(result.trace_id),
        "final_state": result.final_state.value,
        "execution_success": (
            result.execution_report.success if result.execution_report is not None else None
        ),
        "verification_verdict": (
            result.verification_report.verdict.value
            if result.verification_report is not None
            else None
        ),
        "trace_events": len(events),
    }
    typer.echo(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if result.final_state is not TaskState.COMPLETED:
        raise typer.Exit(code=1)


@app.command("show-attempt")
def show_attempt(
    database_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    attempt_id: Annotated[UUID, typer.Argument()],
) -> None:
    """Read a persisted attempt status, result, and trace summary by ID."""

    sink = SQLiteMemoryStore(database_file)
    attempt = sink.get_attempt(attempt_id)
    if attempt is None:
        typer.echo(f"attempt not found: {attempt_id}", err=True)
        raise typer.Exit(code=1)

    memory = sink.reconstruct_attempt(attempt_id)
    execution = attempt.execution_report
    verification = attempt.verification_report
    state_sequence = ["received"]
    state_sequence.extend(
        str(event.payload["to_state"])
        for event in memory.trace_events
        if event.event_type == "orchestrator.transition" and "to_state" in event.payload
    )
    diagnostics = [
        {
            "event_type": event.event_type,
            "payload": dict(event.payload),
        }
        for event in memory.trace_events
        if event.event_type.endswith((".failed", ".rejected"))
        or (
            event.event_type == "orchestrator.transition"
            and event.payload.get("to_state") == "failed"
        )
    ]
    summary = {
        "attempt_id": str(attempt.attempt_id),
        "trace_id": str(attempt.trace_id),
        "task_id": str(attempt.task.task_id),
        "objective": attempt.task.objective,
        "status": attempt.status.value,
        "final_state": attempt.final_state.value if attempt.final_state is not None else None,
        "planner_kind": attempt.plan.planner_kind.value if attempt.plan is not None else None,
        "execution_success": execution.success if execution is not None else None,
        "execution_error": execution.error if execution is not None else None,
        "verification_verdict": verification.verdict.value if verification is not None else None,
        "verification_errors": list(verification.errors) if verification is not None else [],
        "diagnostics": diagnostics,
        "state_sequence": state_sequence,
        "trace_events": len(memory.trace_events),
        "evidence": [item.model_dump(mode="json") for item in memory.evidence],
    }
    typer.echo(json.dumps(summary, ensure_ascii=False, sort_keys=True))


@app.command("benchmark")
def run_benchmark(
    suite_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    seed: Annotated[int, typer.Option(help="Deterministic case-order seed.")] = 0,
    split: Annotated[
        BenchmarkSplit | None,
        typer.Option(help="Run only train, evidence, or holdout cases."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(help="Optional JSON report path."),
    ] = None,
) -> None:
    """Run a reproducible benchmark suite and enforce its quality gate."""

    try:
        suite = BenchmarkSuite.model_validate_json(suite_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as error:
        typer.echo(f"invalid benchmark suite: {error}", err=True)
        raise typer.Exit(code=2) from error

    with tempfile.TemporaryDirectory(prefix="llmin-benchmark-") as temporary_directory:
        report = BenchmarkRunner().run(
            suite,
            run_root=Path(temporary_directory),
            seed=seed,
            selected_split=split,
        )

    report_json = report.model_dump_json(indent=2)
    if output is not None:
        if not output.parent.exists() or not output.parent.is_dir():
            typer.echo("invalid output: parent directory does not exist", err=True)
            raise typer.Exit(code=2)
        output.write_text(report_json + "\n", encoding="utf-8", newline="\n")

    typer.echo(
        json.dumps(
            {
                "suite": report.suite_name,
                "suite_fingerprint": report.suite_fingerprint,
                "observed_outcome_fingerprint": report.observed_outcome_fingerprint,
                "evaluation_fingerprint": report.evaluation_fingerprint,
                "cases": report.metrics.total_cases,
                "matched": report.metrics.matched_cases,
                "unsafe_acceptances": report.metrics.unsafe_acceptances,
                "quality_gate_passed": report.metrics.quality_gate_passed,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if not report.metrics.quality_gate_passed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
