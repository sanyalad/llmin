# Governed Memory

The memory subsystem implements the first executable boundary from manifesto 0.4. It
deliberately separates an append-only evidence journal, complete execution attempts, and
governed memory objects.

## Complete attempts

Every `PipelineResult`, including failures before execution, exposes the pipeline `task_id`,
`trace_id`, `attempt_id`, and the execution plan when one exists. `AttemptRecorder` rejects a
result whose task differs from the supplied `TaskSpec` and turns a matching result into
an `AttemptRecord` containing:

- the complete redacted `TaskSpec`;
- the plan, execution report, and independent verification report when produced;
- a content-derived `EnvironmentRecord`;
- the terminal pipeline state;
- immutable references to persisted artifacts.

Recording has two explicit phases. `begin_attempt()` creates an `open` record, and
`finalize_attempt()` atomically commits terminal metadata and verifier evidence in one SQLite
transaction. `AttemptCoordinator` is the normal pre-run boundary: it creates the open record,
passes its fixed `trace_id` and `attempt_id` to `Pipeline`, then finalizes that same record. A
successful direct `Pipeline.run()` stops at `verified`; only a successful coordinator recording
gate permits the `recorded` and `completed` transitions. Failed persistence therefore leaves the
attempt open and cannot publish a false terminal success. A
crash before a `PipelineResult` leaves the open record for diagnosis instead of creating a
trace-only execution. Repeating either persistence operation with identical content is
idempotent, including direct `begin_attempt()` retries that do not supply a timestamp. Reusing
an identifier for different content is rejected. SQLite identity comparisons use a canonical
JSON encoding that sorts only unordered set-like fields; declared list and tuple order remains
meaningful.

An attempt marked `completed` is valid only when execution succeeded and independent
verification passed. Cross-task, cross-plan, and cross-attempt report references are rejected
before persistence. If finalization fails, SQLite rolls back evidence and leaves the attempt
open rather than presenting a partial record as complete.

## Artifact store

`ContentAddressedArtifactStore` stores redacted UTF-8 artifacts under their SHA-256 digest.
Writes use a temporary file, flush, filesystem sync, atomic replacement, and verification of
both existing and newly read content. A digest collision with different content or later
tampering is rejected.

Stage 1 accepts only `text/plain`, `application/json`, and `application/toml`; JSON and TOML are
parsed before storage. Content rejected by the existing secret-redaction boundary is not stored.
The store enforces per-blob and total-size quotas, validates logical names, and rejects symlink,
junction, and other reparse-point components below its trusted root. Binary artifacts and
media-specific sanitizers are deliberately deferred.

## Evidence journal

`SQLiteMemoryStore` persists:

- ordered, redacted `TraceEvent` records;
- verifier `Evidence` linked to an attempt;
- categorized `CostEntry` records.

Identifiers are immutable. Repeating the same record is idempotent; reusing an identifier
with different content is rejected. `reconstruct_attempt()` returns events in insertion order
and does not turn them into an episode automatically.

SQLite schema version is checked and migrated on every open. Free-form payloads cross the existing
redaction boundary before SQL execution, foreign keys are enabled, and secure deletion is
requested from SQLite. The adapter opens and closes a connection per atomic operation.

## Episodes

An episode is a selected, reproducible description of one attempt. It requires:

- existing trace and evidence provenance from the same task/attempt;
- a content hash calculated after redaction;
- at least one environment fingerprint; when the source attempt is persisted, its fingerprint
  must be included;
- an explicit retention policy;
- a non-empty summary while payload is active.

Allowed states:

```text
active ↔ cold
   ↘       ↘
   quarantined
       ↓
  tombstoned
```

Transitions are append-only audit records with a reason. `tombstoned` is terminal. Payload
deletion before `minimum_retain_until` is rejected. A tombstone keeps identifiers, provenance,
content hash, and retention metadata while removing the episode summary.

## Common artifact contracts

`MemoryArtifact` is the shared identity and trust envelope. It contains artifact kind, state,
content hash, provenance, applicability, retention, and creation time. An artifact represents a
claim with evidence, not a database row that is automatically true.

`Provenance` links source trace events, verifier evidence, verification reports, and parent artifacts.
When report provenance is declared, its IDs must resolve to the source attempt and referenced
evidence must belong to that report; parent episode IDs must already exist. A general artifact
registry is still deferred, so this increment validates persisted episode parents only.
`Applicability` describes family, structured scope, compatible environments, preconditions,
exclusions, and required capabilities. An empty environment set means «not yet constrained by
an environment fingerprint», not «compatible with every future environment».

`ArtifactVerifierResult` links an artifact to the existing domain `VerificationReport`; Memory
does not introduce a competing verifier truth model.

`ArtifactRelation` records typed graph edges without requiring a graph database. SQLite remains
the source of truth. The current increment persists relations between episode artifacts; generic
Rule and Experiment persistence follows in a later increment. `ContradictionRecord` preserves unresolved conflicts
rather than replacing one artifact with another. A resolution is a new immutable record that
supersedes the open investigation, so the conflict history cannot be overwritten.

`ExperimentArtifact` preserves hypotheses and negative results. Its contract exists, while
automatic experiment generation and routing are deferred.

## Recovery boundary

The coordinator makes finalized attempt metadata internally atomic and creates the attempt
before a coordinated pipeline run. It is still not a complete recovery system:

- callers can invoke `Pipeline.run()` directly, but such a run stops at `verified` and is not a
  durably completed attempt;
- an operating-system crash can still interrupt an open attempt and requires reconciliation;
- a failed database finalization may leave an unreferenced immutable CAS blob;
- garbage collection and reconciliation of trace-only attempts or unreferenced blobs are not
  implemented yet.

The next recovery increment should add startup reconciliation and conservative reference-based
garbage collection.

## Explicit non-goals

The memory subsystem does not yet:

- promote episodes to semantic or procedural knowledge;
- persist or route Rule/Experiment artifacts;
- resolve contradictions automatically;
- activate candidates through shadow or canary modes;
- run Memory Economist quarantine decisions;
- summarize traces with an LLM;
- make age-only deletion decisions;
- implement similarity or vector search;
- load episodes into Context Compiler;
- sanitize or store binary artifacts;
- reconcile process crashes across pipeline, SQLite, and the artifact store;
- garbage-collect unreferenced content-addressed blobs;
- claim cryptographic erasure of storage-device remnants.

Those capabilities require separate policy, evaluation, and threat-model increments.
