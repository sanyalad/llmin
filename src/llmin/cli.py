"""Command-line entrypoint for inspecting and validating Stage 1 artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from llmin import __version__
from llmin.domain import TaskSpec

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


if __name__ == "__main__":
    app()
