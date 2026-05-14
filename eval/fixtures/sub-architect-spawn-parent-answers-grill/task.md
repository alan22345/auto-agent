# Task — Sub-architect spawn, parent answers grill (ADR-015 §10)

A genuinely huge task tuned to push the architect through
`spawn_sub_architects` instead of a flat backlog. Sub-architect grill
questions MUST be relayed to the parent architect — never to the user,
the PO standin, or the improvement-agent standin.

> Rewrite the authentication, billing, and notifications subsystems
> across this monolith with consistent observability: every entrypoint
> emits a structured log line with `subsystem`, `actor`, `action`, and
> `correlation_id`. Every external call is wrapped with retries +
> circuit breaker. Every domain event lands on Kafka with the same
> envelope shape. Each subsystem has its own service boundary, its
> own database schema, and its own integration tests. The migration
> must be safe to roll out behind a feature flag and reversible per
> subsystem.

## Expected behaviour

1. The architect emits a `spawn_sub_architects` decision with three
   slices (`auth`, `billing`, `notifications`).
2. At least one sub-architect emits a grill question (e.g., "which
   correlation-id propagation library should I use?").
3. The orchestrator resumes the parent architect's session and writes
   the answer into `.auto-agent/slices/<slice>/grill_answer.json`.
4. The user / freeform standin is NEVER invoked for these relays.

## Pass criterion

The `grill_rounds` log records every relay with
`answerer_source: "parent_architect"`. Any standin or user invocation
on a grill round constitutes a failure (the parent-relay path is
broken).
