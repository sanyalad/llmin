# Knowledge Router vertical slice

## Purpose

Reuse a previously verified procedure without an LLM call only when the current task and environment exactly satisfy the procedure's applicability contract.

## Trust boundary

Memory may propose a plan, but memory does not grant trust.

A selected skill must still pass:

1. local schema validation;
2. capability authorization;
3. sandbox execution;
4. independent verification;
5. durable attempt recording.

A router hit changes only the source of the `ExecutionPlan`. It must not bypass policy, execution isolation, verification, or persistence.

## Minimal flow

```text
TaskSpec + environment
        |
        v
KnowledgeRouter
  | exact compatible ACTIVE skill
  |---------------------------------> local ExecutionPlan
  |
  | miss / rejected candidate
  v
fallback Planner (OpenRouter)
        |
        v
 authorize -> execute -> verify -> persist
```

## Exact-match policy

The first version is intentionally conservative. A skill is eligible only when all of the following match exactly:

- task family;
- normalized task scope fingerprint;
- environment fingerprint;
- required capabilities are present;
- no exclusion is triggered;
- activation state is `ACTIVE`;
- memory state is `ACTIVE`;
- the stored plan validates against current domain contracts.

No fuzzy similarity, embeddings, heuristic scoring, partial environment match, or automatic widening of applicability is allowed in this slice.

## Router result

Every routing decision must produce a structured result containing:

- decision: `hit`, `miss`, or `rejected`;
- selected skill id when applicable;
- deterministic lookup key;
- rejection reasons for considered candidates;
- planner source: `memory` or `fallback`;
- `llm_calls` count.

The decision must also be emitted to the attempt trace.

## Skill creation

A compiled skill may be derived only from an attempt that:

- reached `COMPLETED`;
- has verifier verdict `PASSED`;
- has a persisted execution plan;
- has trace and evidence provenance;
- records the environment fingerprint used for applicability.

The initial compiler copies the verified typed plan and exact applicability data. It does not generalize from one attempt.

## Failure behavior

- Invalid, inactive, incompatible, or excluded skills are rejected locally.
- Ambiguous multiple exact matches are a safe miss unless one deterministic winner is explicitly defined by contract.
- Any router error becomes a safe fallback decision, not execution of an unvalidated skill.
- A fallback planner failure remains a normal planning failure.
- A verifier failure invalidates the current attempt even when the plan came from memory.

## Acceptance tests

1. A completed verified attempt compiles to an exact-match skill with provenance.
2. A compatible repeated task selects that skill and records `llm_calls = 0`.
3. The selected plan still passes authorization, sandbox execution, and independent verification.
4. A different environment fingerprint rejects the skill and calls the fallback planner once.
5. A different task scope rejects the skill and calls the fallback planner once.
6. An exclusion rejects the skill and calls the fallback planner once.
7. An inactive or quarantined skill is never selected.
8. An invalid stored plan is rejected before authorization or execution.
9. Router hit, miss, and rejection reasons are visible in the persisted trace.
10. Existing deterministic fixture, benchmark, memory, and OpenRouter tests remain green.

## Non-goals

- semantic similarity search;
- learned ranking;
- automatic promotion to `ACTIVE`;
- cross-family reuse;
- plan mutation by the router;
- verifier bypass;
- production deployment changes.
