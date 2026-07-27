import hashlib
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from llmin.domain import ExecutionPlan, TaskSpec
from llmin.domain.json_types import canonical_json
from llmin.execution import CapabilityRegistry, Executor, SandboxFactory
from llmin.memory import (
    ArtifactStoreError,
    AttemptRecorder,
    AttemptStatus,
    ContentAddressedArtifactStore,
    EnvironmentRecord,
    MemoryStoreError,
    SQLiteMemoryStore,
    environment_content_hash,
)
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


def test_schema_v2_database_is_migrated_to_v3(tmp_path: Path) -> None:
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
    assert version == 3
    assert {"attempts", "environments"}.issubset(tables)
