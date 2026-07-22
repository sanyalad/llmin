# Memory v0

Memory v0 implements the first executable boundary from manifesto 0.4. It deliberately
separates an append-only evidence journal from governed memory objects.

## Evidence journal

`SQLiteMemoryStore` persists:

- ordered, redacted `TraceEvent` records;
- verifier `Evidence` linked to an attempt;
- categorized `CostEntry` records.

Identifiers are immutable. Repeating the same record is idempotent; reusing an identifier
with different content is rejected. `reconstruct_attempt()` returns events in insertion order
and does not turn them into an episode automatically.

SQLite schema version is checked on every open. Free-form payloads cross the existing
redaction boundary before SQL execution, foreign keys are enabled, and secure deletion is
requested from SQLite. The adapter opens and closes a connection per atomic operation.

## Episodes

An episode is a selected, reproducible description of one attempt. It requires:

- existing trace and evidence provenance from the same task/attempt;
- a content hash calculated after redaction;
- an environment fingerprint;
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
`Applicability` describes family, structured scope, compatible environments, preconditions,
exclusions, and required capabilities. An empty environment set means «not yet constrained by
an environment fingerprint», not «compatible with every future environment».

`ArtifactVerifierResult` links an artifact to the existing domain `VerificationReport`; Memory
does not introduce a competing verifier truth model.

`ArtifactRelation` records typed graph edges without requiring a graph database. SQLite remains
the source of truth. Memory v0 persists relations between episode artifacts; generic Rule and
Experiment persistence follows in M2. `ContradictionRecord` preserves unresolved conflicts
rather than replacing one artifact with another. A resolution is a new immutable record that
supersedes the open investigation, so the conflict history cannot be overwritten.

`ExperimentArtifact` preserves hypotheses and negative results. Its contract exists in Memory
v0, while automatic experiment generation and routing are deferred.

## Explicit non-goals

Memory v0 does not yet:

- promote episodes to semantic or procedural knowledge;
- persist or route Rule/Experiment artifacts;
- resolve contradictions automatically;
- activate candidates through shadow or canary modes;
- run Memory Economist quarantine decisions;
- summarize traces with an LLM;
- make age-only deletion decisions;
- implement similarity or vector search;
- load episodes into Context Compiler;
- claim cryptographic erasure of storage-device remnants.

Those capabilities require separate policy, evaluation, and threat-model increments.
