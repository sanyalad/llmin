# ADR 0002: Memory is a governed lifecycle

- Status: accepted
- Date: 2026-07-22
- Manifesto: 0.3-draft

## Context

LLMIN already emits structured traces and verification evidence. Persisting every event in
one uniformly searchable history would make storage growth look like learning while raising
retrieval cost, contradiction risk, privacy exposure, and prompt noise.

The manifesto defines a compression direction:

```text
event → experience → episode → semantic knowledge → procedural skill
```

The arrows are policy decisions, not automatic promotions. Raw observations remain evidence;
they do not become knowledge merely because they were stored or frequently retrieved.

## Decision

Memory v0 separates two concerns:

1. An append-only evidence journal stores redacted trace events, verifier evidence, and cost
   entries required to reconstruct an attempt.
2. Governed memory objects represent selected episodes and later semantic/procedural artifacts.
   They carry provenance, an explicit retention policy, environment compatibility, and a
   lifecycle state.

The first implementation supports episodic objects only. Semantic and procedural values are
reserved contract layers until crystallization owns their promotion rules.

Allowed episode states are `active`, `cold`, `quarantined`, and `tombstoned`. A tombstone is
retained after payload removal so forgetting remains auditable. State changes are append-only
records and require a reason. The repository never infers that storage frequency implies
reliability.

Retention decisions must distinguish:

- payload retention from evidence retention;
- earliest permitted deletion from optional expiry;
- active indexing from cold storage;
- deletion from quarantine.

All persisted free-form data passes through the same redaction boundary as trace sinks.
SQLite is an adapter behind contracts, not the definition of memory semantics.

## Consequences

- LLM planning is sequenced after the journal foundation so calls and costs are measurable
  from their first integration.
- Attempt reconstruction does not require loading episodes into an LLM context.
- Compaction and deletion need policy tests and provenance checks, not only CRUD tests.
- Physical holdout isolation remains a separate benchmark/crystallization boundary.
- Memory v0 intentionally does not implement similarity search, autonomous summarization,
  semantic promotion, or destructive vacuuming.
