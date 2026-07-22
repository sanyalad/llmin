"""Built-in typed capabilities available to Stage 1 plans."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
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
        except ValidationError as error:
            raise CapabilityError("read_text rejected: invalid arguments") from error
        except SandboxPolicyError as error:
            raise CapabilityError(f"read_text rejected: {error}") from error
        return {
            "path": parsed.path,
            "sha256": hashlib.sha256(content.encode(parsed.encoding)).hexdigest(),
            "characters": len(content),
        }


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
        except ValidationError as error:
            raise CapabilityError("write_text_atomic rejected: invalid arguments") from error
        except SandboxPolicyError as error:
            raise CapabilityError(f"write_text_atomic rejected: {error}") from error
        return {"path": parsed.path, "characters": len(parsed.content)}


class _PatchTomlArguments(_Arguments):
    path: str = Field(min_length=1)
    key: str = Field(pattern=r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
    value: str | bool | int | float


def _toml_scalar(value: str | bool | int | float) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class PatchTomlCapability:
    """Patch one existing scalar TOML key while preserving unrelated formatting."""

    name = "patch_toml"

    def execute(
        self,
        arguments: dict[str, Any],
        transaction: SandboxTransaction,
    ) -> dict[str, Any]:
        try:
            parsed = _PatchTomlArguments.model_validate(arguments)
            original = transaction.read_text(parsed.path)
            key_parts = parsed.key.split(".")
            section = tuple(key_parts[:-1])
            leaf = key_parts[-1]
            current_section: tuple[str, ...] = ()
            replaced = False
            output_lines: list[str] = []
            key_pattern = re.compile(
                rf"^(?P<prefix>\s*{re.escape(leaf)}\s*=\s*)(?P<value>[^#]*?)(?P<suffix>\s*(?:#.*)?)$"
            )
            section_pattern = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")

            for line in original.splitlines(keepends=True):
                line_body = line.rstrip("\r\n")
                newline = line[len(line_body) :]
                section_match = section_pattern.match(line_body)
                if section_match:
                    current_section = tuple(
                        part.strip() for part in section_match.group(1).split(".")
                    )
                if current_section == section:
                    key_match = key_pattern.match(line_body)
                    if key_match:
                        if replaced:
                            raise CapabilityError("patch_toml rejected: duplicate target key")
                        line_body = (
                            key_match.group("prefix")
                            + _toml_scalar(parsed.value)
                            + key_match.group("suffix")
                        )
                        replaced = True
                output_lines.append(line_body + newline)

            if not replaced:
                raise CapabilityError("patch_toml rejected: target key does not exist")
            updated = "".join(output_lines)
            parsed_document = tomllib.loads(updated)
            actual: Any = parsed_document
            for part in key_parts:
                actual = actual[part]
            if actual != parsed.value:
                raise CapabilityError("patch_toml rejected: patched value failed self-check")
            transaction.write_text_atomic(parsed.path, updated)
        except CapabilityError:
            raise
        except ValidationError as error:
            raise CapabilityError("patch_toml rejected: invalid arguments") from error
        except (
            KeyError,
            TypeError,
            SandboxPolicyError,
            tomllib.TOMLDecodeError,
        ) as error:
            raise CapabilityError(f"patch_toml rejected: {error}") from error
        return {"path": parsed.path, "key": parsed.key}
