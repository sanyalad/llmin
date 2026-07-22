"""Append-only structured events with redaction before persistence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import Field

from llmin.domain.models import ContractModel

_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact values whose field names commonly contain secrets."""

    if key is not None and any(fragment in key.casefold() for fragment in _SENSITIVE_KEY_FRAGMENTS):
        return "[REDACTED]"
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


class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> None: ...


class InMemoryTraceSink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        sanitized = event.model_copy(update={"payload": redact(event.payload)})
        self.events.append(sanitized)


class JsonlTraceSink:
    """Thread-safe JSONL sink that creates parent directories on first event."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = Lock()

    def emit(self, event: TraceEvent) -> None:
        sanitized = event.model_copy(update={"payload": redact(event.payload)})
        line = sanitized.model_dump_json() + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
