"""Built-in typed capabilities available to Stage 1 plans."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llmin.execution.sandbox import SandboxPolicyError, SandboxTransaction


class CapabilityError(ValueError):
    pass


class Capability(Protocol):
    name: str

    def execute(
        self,
        arguments: dict[str, Any],
        transaction: SandboxTransaction,
    ) -> dict[str, Any]: ...


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class _ReadTextArguments(_Arguments):
    path: str = Field(min_length=1)
    encoding: str = "utf-8"


class ReadTextCapability:
    name = "read_text"

    def execute(
        self,
        arguments: dict[str, Any],
        transaction: SandboxTransaction,
    ) -> dict[str, Any]:
        try:
            parsed = _ReadTextArguments.model_validate(arguments)
            content = transaction.read_text(parsed.path, encoding=parsed.encoding)
        except (ValidationError, SandboxPolicyError) as error:
            raise CapabilityError(f"read_text rejected: {error}") from error
        return {"path": parsed.path, "content": content, "characters": len(content)}


class _WriteTextArguments(_Arguments):
    path: str = Field(min_length=1)
    content: str
    encoding: str = "utf-8"


class WriteTextAtomicCapability:
    name = "write_text_atomic"

    def execute(
        self,
        arguments: dict[str, Any],
        transaction: SandboxTransaction,
    ) -> dict[str, Any]:
        try:
            parsed = _WriteTextArguments.model_validate(arguments)
            transaction.write_text_atomic(
                parsed.path,
                parsed.content,
                encoding=parsed.encoding,
            )
        except (ValidationError, SandboxPolicyError) as error:
            raise CapabilityError(f"write_text_atomic rejected: {error}") from error
        return {"path": parsed.path, "characters": len(parsed.content)}
