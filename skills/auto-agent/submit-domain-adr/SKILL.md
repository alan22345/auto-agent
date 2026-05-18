---
name: submit-domain-adr
description: Persist a per-domain ADR so the orchestrator can route it to per-domain approval and downstream child trios. Use once you've worked out the bounded context, aggregates, public surface, and integration points for your assigned domain.
---

<what-to-do>

Write your domain ADR to `.auto-agent/adrs/<index>-<slug>.md` in the workspace, then stop.

`<index>` is the zero-padded three-digit number the orchestrator assigned (e.g. `001`, `002`, `003`). `<slug>` is your domain's kebab-case slug — both are given to you in the prompt that invoked you. Substitute the literal values into the path — e.g. for index 1, slug `auth`, the file path is `.auto-agent/adrs/001-auth.md`.

The file is markdown. The validator parses every section header by name, so the shape is load-bearing — match it exactly:

````markdown
# <Domain Name> ADR

## Scope
<≥80 word description of this domain's responsibility. DDD-flavoured. Include the bounded context boundary, key aggregates, invariants, and what is explicitly NOT in this domain.>

## Aggregates and ubiquitous language
- <aggregate or domain term> — <one sentence>
- ...

## Public surface
- Routes: <list of HTTP routes this domain exposes>
- Events: <list of events this domain emits/consumes>
- Public types: <list of public Python types or interfaces>

## Integration points
<how this domain connects to other domains, named explicitly — e.g. "Billing — consumes user.created from Auth">

## Affected routes (for verification)

```yaml
affected_routes:
  - "/api/some/route"
  - "/api/other/route"
```

## Justification
<one paragraph: why this is its own slice and not folded into a sibling domain>
````

Use Write (not Edit) to create the file — create the `.auto-agent/adrs/` directory first if it doesn't exist.

</what-to-do>

<rules>

- All six sections (`## Scope`, `## Aggregates and ubiquitous language`, `## Public surface`, `## Integration points`, `## Affected routes`, `## Justification`) MUST appear by name. The validator rejects ADRs missing any required section.
- The `## Scope` section body MUST be ≥80 whitespace-split words. Shorter and the validator bounces it back for a retry.
- `affected_routes:` is a YAML list inside a fenced ` ```yaml ` block under `## Affected routes`. The final-verification phase reads it to know which routes to exercise.
- Substitute the literal `<index>` and `<slug>` into the file path — do not leave the placeholder text in.
- Do not output the ADR in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/adrs/<index>-<slug>.md` after your turn returns; that is the only signal it needs from you here.

</rules>
