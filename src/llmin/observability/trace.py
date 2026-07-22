"""Append-only structured events with redaction before persistence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from llmin.domain.json_types import FrozenDict, freeze_json_object
from llmin.domain.models import ContractModel

_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "api-key",
    "api_key",
    "apikey",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)

_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?i)\b(?:api[-_ ]?key|password|secret|token)\s*[:=]\s*\S+"),
)


def _redact_string(value: str) -> str:
    redacted = value
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact values whose field names commonly contain secrets."""

    if key is not None and any(fragment in key.casefold() for fragment in _SENSITIVE_KEY_FRAGMENTS):
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        return {
            str(child_key): redact(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


class TraceEvent(ContractModel):
    event_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID
    task_id: UUID
    attempt_id: UUID
    event_type: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.]*$")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    @field_validator("payload", mode="after")
    @classmethod
    def freeze_payload(cls, value: Any) -> FrozenDict:
        return freeze_json_object(value)


class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> None: ...


class InMemoryTraceSink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        sanitized = TraceEvent.model_validate(
            {**event.model_dump(), "payload": redact(event.payload)}
        )
        self.events.append(sanitized)


class JsonlTraceSink:
    """Thread-safe JSONL sink that creates parent directories on first event."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = Lock()

    def emit(self, event: TraceEvent) -> None:
        sanitized = TraceEvent.model_validate(
            {**event.model_dump(), "payload": redact(event.payload)}
        )
        line = sanitized.model_dump_json() + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
