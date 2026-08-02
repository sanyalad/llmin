"""Deterministic runtime environment capture for attempt applicability."""

from __future__ import annotations

import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from llmin import __version__


class EnvironmentProbe:
    """Capture the compatibility-relevant runtime without local workspace paths."""

    def capture(
        self,
        *,
        implementation_revision: str | None = None,
    ) -> dict[str, object]:
        return {
            "runtime": {
                "os": platform.system(),
                "os_release": platform.release(),
                "architecture": platform.machine(),
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
            },
            "implementation": {
                "llmin_version": __version__,
                "revision": implementation_revision or self._implementation_revision(),
            },
            "contracts": {
                "task_spec": "1.0",
                "attempt_record": "1.0",
                "memory_schema": 4,
                "document_encoding": "canonical-json-v1",
            },
            "capabilities": {
                "patch_toml": "stage1-v1",
                "read_text": "stage1-v1",
                "write_text_atomic": "stage1-v1",
            },
            "verifiers": {
                "text_equals": "stage1-v1",
                "toml_value_equals": "stage1-v1",
            },
            "dependencies": {
                "pydantic": self._package_version("pydantic"),
                "typer": self._package_version("typer"),
            },
            "formats": {
                "json": "stdlib",
                "toml": f"tomllib-py{sys.version_info.major}.{sys.version_info.minor}",
                "text": "utf-8",
            },
        }

    @staticmethod
    def _package_version(package: str) -> str:
        try:
            return version(package)
        except PackageNotFoundError:
            return "unknown"

    @staticmethod
    def _implementation_revision() -> str:
        declared = os.environ.get("LLMIN_REVISION")
        if declared:
            return declared
        repository = Path(__file__).resolve().parents[3]
        git_directory = repository / ".git"
        try:
            if git_directory.is_file():
                marker = git_directory.read_text(encoding="utf-8").strip()
                git_directory = (repository / marker.removeprefix("gitdir: ").strip()).resolve()
            head = (git_directory / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: "):
                return (
                    (git_directory / head.removeprefix("ref: ")).read_text(encoding="utf-8").strip()
                )
            return head
        except OSError:
            return "unknown"
