import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from llmin.domain import Evidence
from llmin.memory import (
    Applicability,
    ArtifactRelation,
    ContradictionRecord,
    ContradictionStatus,
    CostCategory,
    CostEntry,
    Episode,
    ExperimentArtifact,
    ExperimentStatus,
    MemoryState,
    MemoryStoreError,
    Provenance,
    RelationKind,
    RetentionPolicy,
    RuleArtifact,
    SQLiteMemoryStore,
    artifact_content_hash,
)
from llmin.observability import TraceEvent

_HASH = "a" * 64
_ENVIRONMENT = "b" * 64
_SUMMARY = "Verified config patch; token=super-secret-value"


def make_store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(tmp_path / "memory.sqlite3")


def make_event(*, attempt_id=None, event_type: str = "test.first") -> TraceEvent:
    return TraceEvent(
        trace_id=uuid4(),
        task_id=uuid4(),
        attempt_id=attempt_id or uuid4(),
        event_type=event_type,
        payload={"authorization": "Bearer secret-token-value", "safe": "visible"},
    )


def make_episode(*, minimum_retain_until: datetime) -> Episode:
    event_id = uuid4()
    evidence_id = uuid4()
    return Episode(
        task_id=uuid4(),
        attempt_id=uuid4(),
        summary=_SUMMARY,
        content_hash=artifact_content_hash(_SUMMARY),
        provenance=Provenance(
            source_event_ids=frozenset({event_id}),
            evidence_ids=frozenset({evidence_id}),
        ),
        applicability=Applicability(
            family="config_patch",
            scope={"file_format": "toml", "schema_version": "1.x"},
            environment_fingerprints=frozenset({_ENVIRONMENT}),
            required_capabilities=frozenset({"patch_toml"}),
        ),
        retention=RetentionPolicy(
            minimum_retain_until=minimum_retain_until,
            reason="verification provenance",
        ),
    )


def persist_episode_provenance(store: SQLiteMemoryStore, episode: Episode) -> None:
    event_id = next(iter(episode.provenance.source_event_ids))
    evidence_id = next(iter(episode.provenance.evidence_ids))
    store.append_trace(
        TraceEvent(
            event_id=event_id,
            trace_id=uuid4(),
            task_id=episode.task_id,
            attempt_id=episode.attempt_id,
            event_type="verification.completed",
            payload={"verdict": "passed"},
        )
    )
    store.append_evidence(
        episode.attempt_id,
        Evidence(
            evidence_id=evidence_id,
            kind="test_file",
            locator="config.toml",
            sha256=_HASH,
        ),
    )


def test_attempt_journal_reconstructs_ordered_redacted_records(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    attempt_id = uuid4()
    first = make_event(attempt_id=attempt_id, event_type="test.first")
    second = make_event(attempt_id=attempt_id, event_type="test.second")
    evidence = Evidence(
        kind="test_file",
        locator="token=super-secret-value",
        sha256=_HASH,
        metadata={"password": "do-not-store", "size": 12},
    )
    cost = CostEntry(
        attempt_id=attempt_id,
        category=CostCategory.VERIFICATION,
        amount_usd=Decimal("0.0025"),
        metadata={"api_key": "sk-abcdefghijklmnopqrstuvwxyz123456"},
    )

    store.emit(first)
    store.emit(second)
    store.append_evidence(attempt_id, evidence)
    store.append_cost(cost)
    reconstructed = store.reconstruct_attempt(attempt_id)

    assert [event.event_type for event in reconstructed.trace_events] == [
        "test.first",
        "test.second",
    ]
    assert reconstructed.trace_events[0].payload["authorization"] == "[REDACTED]"
    assert reconstructed.evidence[0].locator == "[REDACTED]"
    assert reconstructed.evidence[0].metadata["password"] == "[REDACTED]"
    assert reconstructed.costs[0].amount_usd == Decimal("0.0025")
    assert reconstructed.costs[0].metadata["api_key"] == "[REDACTED]"
    raw_database = (tmp_path / "memory.sqlite3").read_bytes()
    assert b"secret-token-value" not in raw_database
    assert b"do-not-store" not in raw_database
    assert b"abcdefghijklmnopqrstuvwxyz123456" not in raw_database


def test_journal_is_idempotent_but_rejects_identifier_reuse(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    event = make_event()

    store.append_trace(event)
    store.append_trace(event)

    changed = event.model_copy(update={"payload": {"safe": "different"}})
    with pytest.raises(MemoryStoreError, match="immutable trace event identifier"):
        store.append_trace(changed)


def test_episode_lifecycle_preserves_auditable_tombstone(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    now = datetime.now(UTC)
    episode = make_episode(minimum_retain_until=now - timedelta(seconds=1))
    persist_episode_provenance(store, episode)
    store.create_episode(episode)

    cold = store.transition_episode(
        episode.episode_id,
        MemoryState.COLD,
        reason="infrequent use",
        occurred_at=now,
    )
    tombstone = store.transition_episode(
        episode.episode_id,
        MemoryState.TOMBSTONED,
        reason="retention expired; password=never-persist",
        occurred_at=now,
    )

    stored = store.get_episode(episode.episode_id)
    assert stored is not None
    assert stored.state is MemoryState.TOMBSTONED
    assert stored.summary is None
    assert stored.content_hash == episode.content_hash
    assert stored.provenance == episode.provenance
    assert [item.transition_id for item in store.episode_history(episode.episode_id)] == [
        cold.transition_id,
        tombstone.transition_id,
    ]
    assert store.episode_history(episode.episode_id)[1].reason == ("retention expired; [REDACTED]")
    with pytest.raises(MemoryStoreError, match="is not allowed"):
        store.transition_episode(
            episode.episode_id,
            MemoryState.ACTIVE,
            reason="cannot resurrect deleted payload",
        )


def test_retention_blocks_early_payload_deletion(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    episode = make_episode(minimum_retain_until=datetime.now(UTC) + timedelta(days=1))
    persist_episode_provenance(store, episode)
    store.create_episode(episode)

    with pytest.raises(MemoryStoreError, match="protected by retention"):
        store.transition_episode(
            episode.episode_id,
            MemoryState.TOMBSTONED,
            reason="premature cleanup",
        )

    stored = store.get_episode(episode.episode_id)
    assert stored is not None
    assert stored.state is MemoryState.ACTIVE
    assert stored.summary == "Verified config patch; [REDACTED]"
    assert stored.content_hash == episode.content_hash
    assert store.episode_history(episode.episode_id) == ()


def test_episode_contract_requires_provenance() -> None:
    payload = make_episode(minimum_retain_until=datetime.now(UTC)).model_dump()
    payload["provenance"] = {"source_event_ids": [], "evidence_ids": []}

    with pytest.raises(ValidationError):
        Episode.model_validate(payload)


def test_rule_and_rejected_experiment_preserve_applicability_and_negative_result() -> None:
    episode = make_episode(minimum_retain_until=datetime.now(UTC))
    provenance = Provenance(parent_artifact_ids=frozenset({episode.artifact_id}))
    applicability = episode.applicability
    retention = episode.retention
    rule = RuleArtifact(
        statement="Use a TOML parser for nested configuration",
        verifier_suite=("toml_value_equals",),
        content_hash=artifact_content_hash("Use a TOML parser for nested configuration"),
        provenance=provenance,
        applicability=applicability,
        retention=retention,
    )
    experiment = ExperimentArtifact(
        hypothesis="Regex is sufficient for nested TOML",
        method="Run nested-table mutation suite",
        outcome="Success rate was 60%",
        status=ExperimentStatus.REJECTED,
        rejection_reason="Breaks on nested structures",
        content_hash=artifact_content_hash("Regex is sufficient for nested TOML"),
        provenance=provenance,
        applicability=applicability,
        retention=retention,
    )

    assert rule.applicability.scope["file_format"] == "toml"
    assert experiment.status is ExperimentStatus.REJECTED
    assert experiment.rejection_reason == "Breaks on nested structures"


def test_rejected_experiment_requires_outcome_and_reason() -> None:
    episode = make_episode(minimum_retain_until=datetime.now(UTC))

    with pytest.raises(ValidationError, match="rejection reason"):
        ExperimentArtifact(
            hypothesis="Regex is sufficient",
            method="Run mutations",
            status=ExperimentStatus.REJECTED,
            content_hash=artifact_content_hash("Regex is sufficient"),
            provenance=Provenance(parent_artifact_ids=frozenset({episode.artifact_id})),
            applicability=episode.applicability,
            retention=episode.retention,
        )


def test_relations_and_contradictions_are_append_only(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = make_episode(minimum_retain_until=datetime.now(UTC))
    second = make_episode(minimum_retain_until=datetime.now(UTC))
    for episode in (first, second):
        persist_episode_provenance(store, episode)
        store.create_episode(episode)
    relation = ArtifactRelation(
        source_artifact_id=second.artifact_id,
        target_artifact_id=first.artifact_id,
        kind=RelationKind.CONTRADICTS,
        reason="Different SDK environment",
    )
    contradiction = ContradictionRecord(
        artifact_ids=frozenset({first.artifact_id, second.artifact_id}),
        applicability=first.applicability,
        description="Rules disagree after an SDK update",
    )

    store.append_relation(relation)
    store.append_relation(relation)
    store.append_contradiction(contradiction)
    resolved = ContradictionRecord(
        artifact_ids=contradiction.artifact_ids,
        applicability=contradiction.applicability,
        status=ContradictionStatus.EXPLAINED,
        description=contradiction.description,
        resolution="SDK version defines two distinct applicability scopes",
        supersedes_contradiction_id=contradiction.contradiction_id,
    )
    store.append_contradiction(resolved)

    assert store.relations() == (relation,)
    assert store.contradictions() == (contradiction, resolved)


def test_retention_contract_rejects_inverted_window() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError, match="cannot precede"):
        RetentionPolicy(
            minimum_retain_until=now,
            expires_at=now - timedelta(seconds=1),
            reason="invalid policy",
        )


def test_episode_creation_rejects_missing_or_mismatched_provenance(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    episode = make_episode(minimum_retain_until=datetime.now(UTC))

    with pytest.raises(MemoryStoreError, match="unknown trace event"):
        store.create_episode(episode)

    wrong_task = episode.model_copy(update={"task_id": uuid4()})
    persist_episode_provenance(store, episode)
    with pytest.raises(MemoryStoreError, match="another task or attempt"):
        store.create_episode(wrong_task)


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    SQLiteMemoryStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_metadata SET version = 999 WHERE singleton = 1")

    with pytest.raises(MemoryStoreError, match="unsupported memory schema version"):
        SQLiteMemoryStore(path)
