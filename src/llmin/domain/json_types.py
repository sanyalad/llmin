"""JSON-compatible immutable containers for auditable contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


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
