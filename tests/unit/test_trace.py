import json
from uuid import uuid4

import pytest

from llmin.observability import InMemoryTraceSink, JsonlTraceSink, TraceEvent, redact


def make_event(payload: dict[str, object]) -> TraceEvent:
    return TraceEvent(
        trace_id=uuid4(),
        task_id=uuid4(),
        attempt_id=uuid4(),
        event_type="test.event",
        payload=payload,
    )


def test_redaction_is_recursive_and_does_not_mutate_input() -> None:
    payload = {
        "token": "abc",
        "nested": {"api_token": "def", "safe": "visible"},
        "items": [{"password": "secret"}],
    }

    result = redact(payload)

    assert result == {
        "token": "[REDACTED]",
        "nested": {"api_token": "[REDACTED]", "safe": "visible"},
        "items": [{"password": "[REDACTED]"}],
    }
    assert payload["token"] == "abc"


def test_memory_sink_redacts_before_storage() -> None:
    sink = InMemoryTraceSink()
    sink.emit(make_event({"authorization": "Bearer value"}))

    assert sink.events[0].payload["authorization"] == "[REDACTED]"


def test_jsonl_sink_writes_one_valid_redacted_event_per_line(tmp_path) -> None:
    path = tmp_path / "traces" / "events.jsonl"
    sink = JsonlTraceSink(path)
    sink.emit(make_event({"secret": "value", "count": 1}))
    sink.emit(make_event({"count": 2}))

    lines = path.read_text(encoding="utf-8").splitlines()
    documents = [json.loads(line) for line in lines]

    assert len(documents) == 2
    assert documents[0]["payload"]["secret"] == "[REDACTED]"
    assert documents[1]["payload"]["count"] == 2


def test_redaction_catches_sensitive_headers_and_string_values() -> None:
    result = redact(
        {
            "X-API-Key": "plain-value",
            "message": "Authorization: Bearer abc.def.ghi",
            "nested": "token=super-secret-value",
        }
    )

    assert result["X-API-Key"] == "[REDACTED]"
    assert "abc.def.ghi" not in result["message"]
    assert "super-secret-value" not in result["nested"]


def test_trace_payload_is_recursively_immutable() -> None:
    event = make_event({"nested": {"value": 1}})

    with pytest.raises(TypeError, match="cannot be mutated"):
        event.payload["nested"]["value"] = 2
