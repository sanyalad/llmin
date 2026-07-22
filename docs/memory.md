# Memory v0

Memory v0 implements the first executable boundary from manifesto 0.3. It deliberately
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

## Explicit non-goals

Memory v0 does not yet:

- promote episodes to semantic or procedural knowledge;
- summarize traces with an LLM;
- make age-only deletion decisions;
- implement similarity or vector search;
- load episodes into Context Compiler;
- claim cryptographic erasure of storage-device remnants.

Those capabilities require separate policy, evaluation, and threat-model increments.
