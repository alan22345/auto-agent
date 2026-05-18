---
name: submit-root-adr
description: Persist the root-architect's system ADR so the orchestrator can present it for approval and parse out the domain list. Use once you've decomposed the product into ≤7 bounded contexts and can write the system-level decisions.
---

<what-to-do>

Write the system ADR to `.auto-agent/adrs/000-system.md` in the workspace, then stop.

The file is markdown. The orchestrator parses the `domains:` YAML block to spawn per-domain architects, so the shape is load-bearing — match it exactly:

````markdown
# System ADR

## Vision
<1-2 paragraphs framing what we're building and why>

## Bounded contexts / domains
<prose describing the high-level slicing — one short paragraph per domain naming the bounded context and what lives inside it>

## Cross-cutting concerns
<auth, observability, deployment, data layer — one paragraph or bullet list per concern>

## Domain list

```yaml
domains:
  - name: <Human-readable domain name>
    slug: <kebab-case-slug>
    scope_summary: <one paragraph: the bounded context, key aggregates, and what this domain owns>
  - name: ...
    slug: ...
    scope_summary: ...
```
````

Use Write (not Edit) to create the file — create the `.auto-agent/adrs/` directory first if it doesn't exist.

</what-to-do>

<rules>

- All four sections (`## Vision`, `## Bounded contexts / domains`, `## Cross-cutting concerns`, `## Domain list`) must be present by name.
- The `domains:` YAML block is the single source of truth the orchestrator parses. Wrap it in a fenced ` ```yaml ` block — no extra prose inside the fence.
- ≤7 domains, hard cap (ADR-018 §3). If you cannot fit the product in ≤7, the product is too broad for one scaffold run; go back and narrow scope.
- Every entry in `domains:` must have `name` (non-empty), `slug` (kebab-case, unique), and `scope_summary` (non-empty paragraph). The validator rejects anything missing one of these.
- Slugs are kebab-case (lowercase, hyphens, no spaces or underscores): `auth`, `user-profile`, `billing`.
- Do not output the ADR in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/adrs/000-system.md` after your turn returns; that is the only signal it needs from you here.

</rules>
