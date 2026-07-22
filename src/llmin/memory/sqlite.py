"""SQLite adapter for the append-only evidence journal and episodic lifecycle."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from llmin.domain import Evidence
from llmin.memory.models import (
    AttemptMemory,
    CostEntry,
    Episode,
    EpisodeTransition,
    MemoryState,
    episode_content_hash,
)
from llmin.observability import TraceEvent, redact

_SCHEMA_VERSION = 1
_ALLOWED_TRANSITIONS = {
    MemoryState.ACTIVE: frozenset(
        {MemoryState.COLD, MemoryState.QUARANTINED, MemoryState.TOMBSTONED}
    ),
    MemoryState.COLD: frozenset(
        {MemoryState.ACTIVE, MemoryState.QUARANTINED, MemoryState.TOMBSTONED}
    ),
    MemoryState.QUARANTINED: frozenset(
        {MemoryState.ACTIVE, MemoryState.COLD, MemoryState.TOMBSTONED}
    ),
    MemoryState.TOMBSTONED: frozenset(),
}


class MemoryStoreError(ValueError):
    pass


class SQLiteMemoryStore:
    def __init__(self, path: Path) -> None:
        if not path.parent.exists() or not path.parent.is_dir():
            raise MemoryStoreError("memory database parent must be an existing directory")
        self.path = path
        self._migrate()

    def append_trace(self, event: TraceEvent) -> None:
        sanitized = TraceEvent.model_validate(
            {**event.model_dump(), "payload": redact(event.payload)}
        )
        document = sanitized.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT document FROM trace_events WHERE event_id = ?",
                (str(sanitized.event_id),),
            ).fetchone()
            if existing is not None:
                self._require_identical(existing[0], document, "trace event")
                return
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM trace_events WHERE attempt_id = ?",
                (str(sanitized.attempt_id),),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO trace_events(event_id, attempt_id, sequence, document) "
                "VALUES (?, ?, ?, ?)",
                (str(sanitized.event_id), str(sanitized.attempt_id), sequence, document),
            )

    def emit(self, event: TraceEvent) -> None:
        """Implement TraceSink so pipelines can persist events at the redaction boundary."""

        self.append_trace(event)

    def append_evidence(self, attempt_id: UUID, evidence: Evidence) -> None:
        sanitized = Evidence.model_validate(redact(evidence.model_dump()))
        document = sanitized.model_dump_json()
        self._insert_immutable(
            table="evidence",
            identifier_column="evidence_id",
            identifier=str(sanitized.evidence_id),
            attempt_id=attempt_id,
            document=document,
            kind="evidence",
        )

    def append_cost(self, cost: CostEntry) -> None:
        sanitized = CostEntry.model_validate(
            {**cost.model_dump(), "metadata": redact(cost.metadata)}
        )
        self._insert_immutable(
            table="cost_entries",
            identifier_column="cost_id",
            identifier=str(sanitized.cost_id),
            attempt_id=sanitized.attempt_id,
            document=sanitized.model_dump_json(),
            kind="cost entry",
        )

    def reconstruct_attempt(self, attempt_id: UUID) -> AttemptMemory:
        with self._connect() as connection:
            event_rows = connection.execute(
                "SELECT document FROM trace_events WHERE attempt_id = ? ORDER BY sequence",
                (str(attempt_id),),
            ).fetchall()
            evidence_rows = connection.execute(
                "SELECT document FROM evidence WHERE attempt_id = ? ORDER BY rowid",
                (str(attempt_id),),
            ).fetchall()
            cost_rows = connection.execute(
                "SELECT document FROM cost_entries WHERE attempt_id = ? ORDER BY rowid",
                (str(attempt_id),),
            ).fetchall()
        return AttemptMemory(
            attempt_id=attempt_id,
            trace_events=tuple(TraceEvent.model_validate_json(row[0]) for row in event_rows),
            evidence=tuple(Evidence.model_validate_json(row[0]) for row in evidence_rows),
            costs=tuple(CostEntry.model_validate_json(row[0]) for row in cost_rows),
        )

    def create_episode(self, episode: Episode) -> None:
        if episode.state is not MemoryState.ACTIVE:
            raise MemoryStoreError("new episodes must start active")
        sanitized = Episode.model_validate(redact(episode.model_dump()))
        if sanitized.summary is None or sanitized.content_hash != episode_content_hash(
            sanitized.summary
        ):
            raise MemoryStoreError("episode content hash does not match sanitized payload")
        document = sanitized.model_dump_json()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT document FROM episodes WHERE episode_id = ?",
                (str(sanitized.episode_id),),
            ).fetchone()
            if existing is not None:
                self._require_identical(existing[0], document, "episode")
                return
            self._validate_episode_provenance(connection, sanitized)
            connection.execute(
                "INSERT INTO episodes(episode_id, state, document) VALUES (?, ?, ?)",
                (str(sanitized.episode_id), sanitized.state.value, document),
            )

    def get_episode(self, episode_id: UUID) -> Episode | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM episodes WHERE episode_id = ?",
                (str(episode_id),),
            ).fetchone()
        return Episode.model_validate_json(row[0]) if row is not None else None

    def transition_episode(
        self,
        episode_id: UUID,
        target: MemoryState,
        *,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> EpisodeTransition:
        timestamp = occurred_at or datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT document FROM episodes WHERE episode_id = ?",
                (str(episode_id),),
            ).fetchone()
            if row is None:
                raise MemoryStoreError("episode does not exist")
            episode = Episode.model_validate_json(row[0])
            if target not in _ALLOWED_TRANSITIONS[episode.state]:
                raise MemoryStoreError(
                    f"memory transition {episode.state.value} -> {target.value} is not allowed"
                )
            if (
                target is MemoryState.TOMBSTONED
                and timestamp < episode.retention.minimum_retain_until
            ):
                raise MemoryStoreError("episode payload is still protected by retention policy")
            transition = EpisodeTransition(
                episode_id=episode_id,
                from_state=episode.state,
                to_state=target,
                reason=str(redact(reason)),
                occurred_at=timestamp,
            )
            updated = episode.model_copy(
                update={
                    "state": target,
                    "summary": None if target is MemoryState.TOMBSTONED else episode.summary,
                }
            )
            connection.execute(
                "UPDATE episodes SET state = ?, document = ? WHERE episode_id = ?",
                (target.value, updated.model_dump_json(), str(episode_id)),
            )
            connection.execute(
                "INSERT INTO episode_transitions(transition_id, episode_id, document) "
                "VALUES (?, ?, ?)",
                (
                    str(transition.transition_id),
                    str(episode_id),
                    transition.model_dump_json(),
                ),
            )
        return transition

    def episode_history(self, episode_id: UUID) -> tuple[EpisodeTransition, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document FROM episode_transitions WHERE episode_id = ? ORDER BY rowid",
                (str(episode_id),),
            ).fetchall()
        return tuple(EpisodeTransition.model_validate_json(row[0]) for row in rows)

    @staticmethod
    def _validate_episode_provenance(
        connection: sqlite3.Connection,
        episode: Episode,
    ) -> None:
        for event_id in episode.source_event_ids:
            row = connection.execute(
                "SELECT attempt_id, document FROM trace_events WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
            if row is None:
                raise MemoryStoreError("episode references an unknown trace event")
            event = TraceEvent.model_validate_json(row[1])
            if row[0] != str(episode.attempt_id) or event.task_id != episode.task_id:
                raise MemoryStoreError(
                    "episode trace provenance belongs to another task or attempt"
                )
        for evidence_id in episode.evidence_ids:
            row = connection.execute(
                "SELECT attempt_id FROM evidence WHERE evidence_id = ?",
                (str(evidence_id),),
            ).fetchone()
            if row is None:
                raise MemoryStoreError("episode references unknown evidence")
            if row[0] != str(episode.attempt_id):
                raise MemoryStoreError("episode evidence belongs to another attempt")

    def _insert_immutable(
        self,
        *,
        table: str,
        identifier_column: str,
        identifier: str,
        attempt_id: UUID,
        document: str,
        kind: str,
    ) -> None:
        if table not in {"evidence", "cost_entries"}:
            raise MemoryStoreError("unsupported journal table")
        with self._connect() as connection:
            existing = connection.execute(
                f"SELECT document FROM {table} WHERE {identifier_column} = ?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                self._require_identical(existing[0], document, kind)
                return
            connection.execute(
                f"INSERT INTO {table}({identifier_column}, attempt_id, document) VALUES (?, ?, ?)",
                (identifier, str(attempt_id), document),
            )

    @staticmethod
    def _require_identical(existing: str, candidate: str, kind: str) -> None:
        if json.loads(existing) != json.loads(candidate):
            raise MemoryStoreError(f"immutable {kind} identifier was reused with new content")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trace_events (
                    event_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    document TEXT NOT NULL,
                    UNIQUE(attempt_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cost_entries (
                    cost_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS episode_transitions (
                    transition_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
                    document TEXT NOT NULL
                );
                """
            )
            row = connection.execute(
                "SELECT version FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_metadata(singleton, version) VALUES (1, ?)",
                    (_SCHEMA_VERSION,),
                )
            elif row[0] != _SCHEMA_VERSION:
                raise MemoryStoreError(f"unsupported memory schema version: {row[0]}")
