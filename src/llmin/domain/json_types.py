"""JSON-compatible immutable containers for auditable contracts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class FrozenDict(dict[str, Any]):
    """A JSON-serializable dict that rejects mutation after construction."""

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        dict.__init__(self, values or {})

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen JSON objects cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> FrozenDict:
        return self

    def copy(self) -> FrozenDict:
        return self


def freeze_json(value: Any) -> Any:
    """Validate JSON compatibility and recursively freeze containers."""

    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return FrozenDict({key: freeze_json(child) for key, child in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_json(child) for child in value)
    raise ValueError(f"value is not JSON-compatible: {type(value).__name__}")


def freeze_json_object(value: Any) -> FrozenDict:
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenDict):
        raise ValueError("expected a JSON object")
    return frozen


def canonical_json_bytes(value: Any) -> bytes:
    """Return a deterministic JSON representation suitable for identity checks.

    Lists and tuples retain their declared order. Unordered Python sets are sorted by
    each item's own canonical representation before being encoded.
    """

    def normalize(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return normalize(item.model_dump(mode="python"))
        if item is None or isinstance(item, str | bool | int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("canonical JSON numbers must be finite")
            return item
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, Enum):
            return normalize(item.value)
        if isinstance(item, datetime | date | time):
            return item.isoformat()
        if isinstance(item, Mapping):
            return {str(key): normalize(child) for key, child in item.items()}
        if isinstance(item, list | tuple):
            return [normalize(child) for child in item]
        if isinstance(item, set | frozenset):
            normalized = [normalize(child) for child in item]
            return sorted(
                normalized,
                key=lambda child: json.dumps(
                    child,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        raise ValueError(f"value is not canonically JSON-serializable: {type(item).__name__}")

    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
