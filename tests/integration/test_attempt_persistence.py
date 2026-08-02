import hashlib
import inspect
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from llmin.domain import ExecutionPlan, TaskSpec
from llmin.domain.json_types import canonical_json
from llmin.execution import CapabilityRegistry, Executor, SandboxFactory
from llmin.memory import (
    ArtifactStoreError,
    AttemptCoordinator,
    AttemptRecorder,
    AttemptStatus,
    ContentAddressedArtifactStore,
    EnvironmentProbe,
    EnvironmentRecord,
    MemoryStoreError,
    SQLiteMemoryStore,
    environment_content_hash,
)
from llmin.observability import InMemoryTraceSink
from llmin.orchestrator import TaskState
from llmin.pipeline import Pipeline, PipelineResult
from llmin.planning import FakePlanner
from llmin.verification import VerificationService, VerifierRegistry


def load_fixture(tmp_path: Path) -> tuple[TaskSpec, ExecutionPlan, Path]:
    task = TaskSpec.model_validate_json(
        Path("benchmarks/tasks/config_patch/001.json").read_text(encoding="utf-8")
    )
    plan = ExecutionPlan.model_validate_json(
        Path("benchmarks/plans/config_patch/001.json").read_text(encoding="utf-8")
    )
    workspace = tmp_path / task.workspace
    workspace.parent.mkdir(parents=True)
    shutil.copytree(Path("benchmarks/workspaces/config-patch-001"), workspace)
    return task, plan, workspace


def run_fixture(
    tmp_path: Path,
    memory: SQLiteMemoryStore,
) -> tuple[TaskSpec, Path, PipelineResult]:
    task, plan, workspace = load_fixture(tmp_path)
    factory = SandboxFactory(tmp_path)
    pipeline = Pipeline(
        planner=FakePlanner(lambda _task: plan),
        executor=Executor(
            CapabilityRegistry.with_builtins(),
            sandbox_factory=factory,
            trace_sink=memory,
        ),
        verification=VerificationService(
            VerifierRegistry.with_builtins(),
            sandbox_factory=factory,
            trace_sink=memory,
        ),
        trace_sink=memory,
    )
    return task, workspace, pipeline.run(task)


def test_recorder_reconstructs_complete_verified_attempt(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    task, workspace, result = run_fixture(tmp_path, memory)
    result = replace(result, final_state=TaskState.COMPLETED)
    recorder = AttemptRecorder(memory=memory, artifacts=artifacts)
    output = (workspace / "config.toml").read_bytes()

    recorded = recorder.record(
        task=task,
        result=result,
        environment_attributes={
            "os": "test",
            "python": "3.12",
            "api_key": "sk-abcdefghijklmnopqrstuvwxyz123456",
        },
        artifact_payloads={"config.toml": (output, "application/toml")},
    )
    retried = recorder.record(
        task=task,
        result=result,
        environment_attributes={
            "os": "test",
            "python": "3.12",
            "api_key": "sk-abcdefghijklmnopqrstuvwxyz123456",
        },
        artifact_payloads={"config.toml": (output, "application/toml")},
    )

    assert recorded == retried
    assert recorded.status is AttemptStatus.FINALIZED
    assert recorded.final_state is TaskState.COMPLETED
    assert recorded.trace_id == result.trace_id
    assert recorded.attempt_id == result.attempt_id
    assert recorded.plan == result.execution_plan
    assert recorded.execution_report == result.execution_report
    assert recorded.verification_report == result.verification_report
    assert recorded.environment.attributes["api_key"] == "[REDACTED]"
    assert artifacts.read(recorded.artifacts[0]) == output
    journal = memory.reconstruct_attempt(result.attempt_id)
    assert journal.trace_events
    assert journal.evidence == result.verification_report.evidence
    assert b"abcdefghijklmnopqrstuvwxyz123456" not in (tmp_path / "memory.sqlite3").read_bytes()


def test_artifact_store_rejects_secrets_and_detects_tampering(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ArtifactStoreError, match="requiring redaction"):
        store.put(
            b"token=super-secret-value\n",
            logical_name="secret.txt",
            media_type="text/plain",
        )

    blob = store.put(b"safe = true\n", logical_name="config.toml", media_type="application/toml")
    path = store.root / blob.sha256[:2] / blob.sha256
    if path.exists() and path.stat().st_mode:
        path.chmod(0o600)
    path.write_bytes(b"tampered\n")
    with pytest.raises(ArtifactStoreError, match="does not match"):
        store.read(blob)


def test_artifact_store_enforces_declared_format_and_quota(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(
        tmp_path / "artifacts", max_blob_bytes=32, max_total_bytes=34
    )

    with pytest.raises(ArtifactStoreError, match="declared application/json"):
        store.put(b"not json", logical_name="payload.json", media_type="application/json")
    with pytest.raises(ArtifactStoreError, match="per-blob quota"):
        store.put(b"x" * 33, logical_name="large.txt")

    store.put(b"one", logical_name="one.txt")
    with pytest.raises(ArtifactStoreError, match="total quota"):
        store.put(b"x" * 32, logical_name="two.txt")


@pytest.mark.parametrize(
    ("payload", "media_type"),
    [
        (b'{"authorization":"Basic dXNlcjpwYXNz"}', "application/json"),
        (b'[service]\ncookie = "session-value"\n', "application/toml"),
    ],
)
def test_artifact_store_rejects_structured_secret_keys(
    tmp_path: Path,
    payload: bytes,
    media_type: str,
) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ArtifactStoreError, match="requiring redaction"):
        store.put(payload, logical_name="structured.txt", media_type=media_type)


@pytest.mark.parametrize("logical_name", ["a//b.txt", "a/./b.txt", "C:/file.txt", "a/\x00.txt"])
def test_artifact_store_rejects_non_normalized_logical_names(
    tmp_path: Path,
    logical_name: str,
) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ArtifactStoreError, match="logical name"):
        store.put(b"safe", logical_name=logical_name)


def test_artifact_store_rejects_symlink_shard(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")
    payload = b"safe"
    digest = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "outside"
    target.mkdir()
    shard = store.root / digest[:2]
    try:
        shard.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows configuration")

    with pytest.raises(ArtifactStoreError, match="symlink or reparse point"):
        store.put(payload, logical_name="safe.txt")


def test_failed_finalization_leaves_attempt_open_and_evidence_absent(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    task, _workspace, result = run_fixture(tmp_path, memory)
    environment = EnvironmentRecord(
        fingerprint=environment_content_hash({"os": "test"}),
        attributes={"os": "test"},
    )
    memory.begin_attempt(
        attempt_id=result.attempt_id,
        trace_id=result.trace_id,
        task=task,
        plan=result.execution_plan,
        environment=environment,
    )
    invalid_report = result.verification_report.model_copy(update={"attempt_id": uuid4()})

    with pytest.raises(ValidationError, match="another attempt"):
        memory.finalize_attempt(
            result.attempt_id,
            final_state=result.final_state,
            execution_report=result.execution_report,
            verification_report=invalid_report,
        )

    stored = memory.get_attempt(result.attempt_id)
    assert stored is not None and stored.status is AttemptStatus.OPEN
    assert memory.reconstruct_attempt(result.attempt_id).evidence == ()


def test_rejected_artifact_leaves_retryable_open_attempt(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    task, _workspace, result = run_fixture(tmp_path, memory)
    result = replace(result, final_state=TaskState.COMPLETED)
    recorder = AttemptRecorder(memory=memory, artifacts=artifacts)

    with pytest.raises(ArtifactStoreError, match="requiring redaction"):
        recorder.record(
            task=task,
            result=result,
            environment_attributes={"os": "test"},
            artifact_payloads={"unsafe.txt": (b"password=do-not-store\n", "text/plain")},
        )

    stored = memory.get_attempt(result.attempt_id)
    assert stored is not None and stored.status is AttemptStatus.OPEN


def test_begin_attempt_is_idempotent_without_explicit_timestamp(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    task, _workspace, result = run_fixture(tmp_path, memory)
    environment = EnvironmentRecord(
        fingerprint=environment_content_hash({"os": "test"}),
        attributes={"os": "test"},
    )

    first = memory.begin_attempt(
        attempt_id=result.attempt_id,
        trace_id=result.trace_id,
        task=task,
        plan=result.execution_plan,
        environment=environment,
    )
    second = memory.begin_attempt(
        attempt_id=result.attempt_id,
        trace_id=result.trace_id,
        task=task,
        plan=result.execution_plan,
        environment=environment,
    )

    assert first == second


def test_coordinator_opens_attempt_before_planning_and_closes_identity_chain(
    tmp_path: Path,
) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    task, plan, workspace = load_fixture(tmp_path)
    attempt_id = uuid4()
    trace_id = uuid4()

    class InspectingPlanner:
        def plan(self, received_task: TaskSpec) -> ExecutionPlan:
            stored = memory.get_attempt(attempt_id)
            assert stored is not None
            assert stored.status is AttemptStatus.OPEN
            assert stored.task == received_task
            assert stored.trace_id == trace_id
            assert stored.plan is None
            return plan

    factory = SandboxFactory(tmp_path)
    pipeline = Pipeline(
        planner=InspectingPlanner(),
        executor=Executor(
            CapabilityRegistry.with_builtins(), sandbox_factory=factory, trace_sink=memory
        ),
        verification=VerificationService(
            VerifierRegistry.with_builtins(), sandbox_factory=factory, trace_sink=memory
        ),
        trace_sink=memory,
    )

    coordinated = AttemptCoordinator(memory=memory, artifacts=artifacts).run(
        pipeline=pipeline,
        task=task,
        environment_attributes={"os": "test"},
        artifact_collector=lambda _result: {
            "config.toml": ((workspace / "config.toml").read_bytes(), "application/toml")
        },
        trace_id=trace_id,
        attempt_id=attempt_id,
    )

    assert coordinated.result.attempt_id == attempt_id
    assert coordinated.record.status is AttemptStatus.FINALIZED
    assert coordinated.record.plan == plan
    assert coordinated.receipt.attempt_id == attempt_id
    assert memory.get_recording_receipt(attempt_id) == coordinated.receipt
    assert (
        artifacts.read(coordinated.record.artifacts[0]) == (workspace / "config.toml").read_bytes()
    )
    assert b"timeout = 30" in artifacts.read(coordinated.record.artifacts[0])
    journal = memory.reconstruct_attempt(attempt_id)
    assert journal.trace_events
    assert all(event.task_id == task.task_id for event in journal.trace_events)
    assert all(event.trace_id == trace_id for event in journal.trace_events)
    assert all(event.attempt_id == attempt_id for event in journal.trace_events)


def test_coordinator_leaves_open_attempt_when_pipeline_crashes(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    task, plan, _workspace = load_fixture(tmp_path)
    attempt_id = uuid4()

    class FailingTraceSink:
        def emit(self, _event: object) -> None:
            raise RuntimeError("simulated trace sink failure")

    factory = SandboxFactory(tmp_path)
    pipeline = Pipeline(
        planner=FakePlanner(lambda _task: plan),
        executor=Executor(
            CapabilityRegistry.with_builtins(),
            sandbox_factory=factory,
            trace_sink=FailingTraceSink(),
        ),
        verification=VerificationService(
            VerifierRegistry.with_builtins(), sandbox_factory=factory, trace_sink=FailingTraceSink()
        ),
        trace_sink=FailingTraceSink(),
    )

    with pytest.raises(RuntimeError, match="trace sink failure"):
        AttemptCoordinator(memory=memory, artifacts=artifacts).run(
            pipeline=pipeline,
            task=task,
            environment_attributes={"os": "test"},
            attempt_id=attempt_id,
        )

    stored = memory.get_attempt(attempt_id)
    assert stored is not None
    assert stored.status is AttemptStatus.OPEN
    assert stored.plan is None


def test_persistence_failure_never_emits_recorded_or_completed(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    task, plan, _workspace = load_fixture(tmp_path)
    attempt_id = uuid4()
    factory = SandboxFactory(tmp_path)
    pipeline = Pipeline(
        planner=FakePlanner(lambda _task: plan),
        executor=Executor(
            CapabilityRegistry.with_builtins(), sandbox_factory=factory, trace_sink=memory
        ),
        verification=VerificationService(
            VerifierRegistry.with_builtins(), sandbox_factory=factory, trace_sink=memory
        ),
        trace_sink=memory,
    )

    with pytest.raises(ArtifactStoreError, match="requiring redaction"):
        AttemptCoordinator(memory=memory, artifacts=artifacts).run(
            pipeline=pipeline,
            task=task,
            environment_attributes={"os": "test"},
            artifact_collector=lambda _result: {
                "unsafe.txt": (b"password=do-not-store\n", "text/plain"),
            },
            attempt_id=attempt_id,
        )

    stored = memory.get_attempt(attempt_id)
    assert stored is not None
    assert stored.status is AttemptStatus.OPEN
    transitions = [
        event.payload["to_state"]
        for event in memory.reconstruct_attempt(attempt_id).trace_events
        if event.event_type == "orchestrator.transition"
    ]
    assert transitions[-1] == TaskState.VERIFIED.value
    assert TaskState.RECORDED.value not in transitions
    assert TaskState.COMPLETED.value not in transitions


def test_terminal_trace_failure_rolls_back_attempt_and_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    task, plan, _workspace = load_fixture(tmp_path)
    attempt_id = uuid4()
    original_insert_trace = memory._insert_trace

    def fail_recorded_transition(connection: object, event: object) -> None:
        if (
            getattr(event, "event_type", None) == "orchestrator.transition"
            and getattr(event, "payload", {}).get("to_state") == TaskState.RECORDED.value
        ):
            raise RuntimeError("post-verified terminal trace failure")
        original_insert_trace(connection, event)

    monkeypatch.setattr(memory, "_insert_trace", fail_recorded_transition)
    factory = SandboxFactory(tmp_path)
    pipeline = Pipeline(
        planner=FakePlanner(lambda _task: plan),
        executor=Executor(
            CapabilityRegistry.with_builtins(), sandbox_factory=factory, trace_sink=memory
        ),
        verification=VerificationService(
            VerifierRegistry.with_builtins(), sandbox_factory=factory, trace_sink=memory
        ),
        trace_sink=memory,
    )

    with pytest.raises(RuntimeError, match="terminal trace failure"):
        AttemptCoordinator(memory=memory, artifacts=artifacts).run(
            pipeline=pipeline,
            task=task,
            environment_attributes={"os": "test"},
            attempt_id=attempt_id,
        )

    stored = memory.get_attempt(attempt_id)
    assert stored is not None and stored.status is AttemptStatus.OPEN
    transitions = [
        event.payload["to_state"]
        for event in memory.reconstruct_attempt(attempt_id).trace_events
        if event.event_type == "orchestrator.transition"
    ]
    assert transitions[-1] == TaskState.VERIFIED.value
    assert TaskState.RECORDED.value not in transitions
    assert TaskState.COMPLETED.value not in transitions


def test_pipeline_has_no_public_completion_callback() -> None:
    assert "completion_gate" not in inspect.signature(Pipeline.run).parameters


def test_coordinator_rejects_pipeline_using_another_trace_sink(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    task, plan, _workspace = load_fixture(tmp_path)
    other_sink = InMemoryTraceSink()
    factory = SandboxFactory(tmp_path)
    pipeline = Pipeline(
        planner=FakePlanner(lambda _task: plan),
        executor=Executor(
            CapabilityRegistry.with_builtins(),
            sandbox_factory=factory,
            trace_sink=other_sink,
        ),
        verification=VerificationService(
            VerifierRegistry.with_builtins(),
            sandbox_factory=factory,
            trace_sink=other_sink,
        ),
        trace_sink=other_sink,
    )

    with pytest.raises(MemoryStoreError, match="persisted lifecycle trace"):
        AttemptCoordinator(memory=memory, artifacts=artifacts).run(
            pipeline=pipeline,
            task=task,
            environment_attributes={"os": "test"},
        )


def test_storage_rejects_finalized_nonterminal_attempt(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    task, _workspace, result = run_fixture(tmp_path, memory)
    environment = EnvironmentRecord(
        fingerprint=environment_content_hash({"os": "test"}),
        attributes={"os": "test"},
    )
    memory.begin_attempt(
        attempt_id=result.attempt_id,
        trace_id=result.trace_id,
        task=task,
        plan=result.execution_plan,
        environment=environment,
    )

    with pytest.raises(ValidationError, match="terminal final state"):
        memory.finalize_attempt(
            result.attempt_id,
            final_state=TaskState.VERIFIED,
            execution_report=result.execution_report,
            verification_report=result.verification_report,
        )


def test_environment_probe_captures_compatibility_versions_without_local_path() -> None:
    attributes = EnvironmentProbe().capture()

    assert attributes["runtime"]["os"]
    assert attributes["runtime"]["architecture"]
    assert attributes["runtime"]["python_version"]
    assert attributes["implementation"]["llmin_version"]
    assert attributes["contracts"]["memory_schema"] == 4
    assert attributes["capabilities"]["patch_toml"] == "stage1-v1"
    assert attributes["verifiers"]["toml_value_equals"] == "stage1-v1"
    assert "base_root" not in str(attributes)


def test_recorder_rejects_result_for_another_task(tmp_path: Path) -> None:
    memory = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    task, _workspace, result = run_fixture(tmp_path, memory)
    foreign_task = task.model_copy(update={"task_id": uuid4()})

    with pytest.raises(MemoryStoreError, match="another task"):
        AttemptRecorder(memory=memory, artifacts=artifacts).record(
            task=foreign_task,
            result=result,
            environment_attributes={"os": "test"},
        )


def test_canonical_contract_serialization_sorts_set_fields(tmp_path: Path) -> None:
    task, _workspace, _result = run_fixture(
        tmp_path, SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    )
    first = task.model_copy(
        update={
            "constraints": task.constraints.model_copy(
                update={"allowed_capabilities": frozenset({"b", "a"})}
            )
        }
    )
    second = task.model_copy(
        update={
            "constraints": task.constraints.model_copy(
                update={"allowed_capabilities": frozenset({"a", "b"})}
            )
        }
    )

    assert canonical_json(first) == canonical_json(second)


def test_schema_v2_database_is_migrated_to_v4(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    SQLiteMemoryStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE attempts")
        connection.execute("DROP TABLE environments")
        connection.execute("UPDATE schema_metadata SET version = 2 WHERE singleton = 1")

    SQLiteMemoryStore(path)

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT version FROM schema_metadata WHERE singleton = 1"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert version == 4
    assert {"attempts", "environments", "recording_receipts"}.issubset(tables)
