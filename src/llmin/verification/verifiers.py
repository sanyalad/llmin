"""Built-in verifiers that inspect outputs independently from capabilities."""

from __future__ import annotations

import hashlib
import tomllib
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llmin.domain import Evidence, Postcondition
from llmin.execution.sandbox import Sandbox, SandboxPolicyError


class VerificationMismatch(ValueError):
    def __init__(self, message: str, *, evidence: Evidence) -> None:
        super().__init__(message)
        self.evidence = evidence


class VerifierError(ValueError):
    pass


class Verifier(Protocol):
    postcondition_type: str

    def verify(self, postcondition: Postcondition, sandbox: Sandbox) -> Evidence: ...


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _file_evidence(*, kind: str, path: str, content: bytes) -> Evidence:
    return Evidence(
        kind=kind,
        locator=path,
        sha256=hashlib.sha256(content).hexdigest(),
        metadata={"size_bytes": len(content)},
    )


class _TextEqualsParameters(_Parameters):
    path: str = Field(min_length=1)
    value: str


class TextEqualsVerifier:
    postcondition_type = "text_equals"

    def verify(self, postcondition: Postcondition, sandbox: Sandbox) -> Evidence:
        try:
            parameters = _TextEqualsParameters.model_validate(postcondition.parameters)
            path = sandbox.resolve_read(parameters.path)
            content = path.read_bytes()
            actual = content.decode("utf-8")
        except (OSError, UnicodeError, ValidationError, SandboxPolicyError) as error:
            raise VerifierError(f"text_equals could not inspect output: {error}") from error
        evidence = _file_evidence(kind="text_file", path=parameters.path, content=content)
        if actual != parameters.value:
            raise VerificationMismatch(
                "text content does not equal expected value",
                evidence=evidence,
            )
        return evidence


class _TomlValueEqualsParameters(_Parameters):
    path: str = Field(min_length=1)
    key: str = Field(pattern=r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
    value: str | bool | int | float


class TomlValueEqualsVerifier:
    postcondition_type = "toml_value_equals"

    def verify(self, postcondition: Postcondition, sandbox: Sandbox) -> Evidence:
        try:
            parameters = _TomlValueEqualsParameters.model_validate(postcondition.parameters)
            path = sandbox.resolve_read(parameters.path)
            content = path.read_bytes()
            document: Any = tomllib.loads(content.decode("utf-8"))
            for part in parameters.key.split("."):
                document = document[part]
        except (
            KeyError,
            OSError,
            TypeError,
            UnicodeError,
            ValidationError,
            SandboxPolicyError,
            tomllib.TOMLDecodeError,
        ) as error:
            raise VerifierError(f"toml_value_equals could not inspect output: {error}") from error
        evidence = _file_evidence(kind="toml_file", path=parameters.path, content=content)
        if document != parameters.value:
            raise VerificationMismatch(
                "TOML value does not equal expected value",
                evidence=evidence,
            )
        return evidence
