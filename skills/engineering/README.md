# Engineering Skills

Vendored from [mattpocock/skills](https://github.com/mattpocock/skills/tree/main/skills/engineering).

These are loadable via the agent's `skill` tool (see `agent/tools/skill.py`).
The `skills/engineering/` directory takes precedence over `superpowers/skills/`
on name collision.

## Available skills

- **diagnose** — Disciplined diagnosis loop for hard bugs and performance regressions: reproduce → minimise → hypothesise → instrument → fix → regression-test.
- **grill-with-docs** — Grilling session that challenges your plan against the existing domain model, sharpens terminology, and updates `CONTEXT.md` and ADRs inline.
- **improve-codebase-architecture** — Find deepening opportunities in a codebase, informed by `CONTEXT.md` domain language and `docs/decisions/` ADRs. Vocabulary: module, interface, seam, adapter, depth, leverage, locality.
- **tdd** — Test-driven development with red-green-refactor. Vertical slices, not horizontal — one test → one minimal implementation → repeat.
- **zoom-out** — Step up a layer of abstraction when unfamiliar with an area of code.
- **triage** — Triage issues through a state machine of triage roles.
- **to-prd** — Turn the current conversation context into a PRD and submit it as a GitHub issue.
- **to-issues** — Break any plan, spec, or PRD into independently-grabbable GitHub issues using vertical slices.

## Local rewrites

Two universal rewrites were applied when vendoring:

- `docs/adr/` → `docs/decisions/` (this repo's ADR location).
- "the Agent tool" (referring to parallel-worker dispatch) → "the `subagent` tool" (this repo's tool name).

A 2-line note in `grill-with-docs/ADR-FORMAT.md` points to this repo's ADR
template at `docs/decisions/000-template.md`.

## How they're woven into the agent

- `improve-codebase-architecture` — vocabulary & lens baked into the agent's
  system prompt (`agent/context/system.py`) and into the coding `_CRITICAL_RULES`,
  so every coding turn is judged through it.
- `grill-with-docs` — invoked automatically before any plan is written for
  complex tasks, via a multi-round Q&A loop using the existing
  `awaiting_clarification` state.
- `tdd` — auto-loaded when the task's `change_type` intent is `feature`,
  `refactor`, or `performance`.
- `diagnose` — auto-loaded when the task's `change_type` intent is `bugfix`.
- The remaining skills (`zoom-out`, `triage`, `to-prd`, `to-issues`) are
  loadable on demand by the agent via the `skill` tool.

The `improve-codebase-architecture` skill also drives the new per-repo
**Architecture Mode** (analogous to Freeform Mode) — see
`agent/architect_analyzer.py`.

## Attribution

Original content © Matt Pocock, vendored from
<https://github.com/mattpocock/skills>. Local rewrites are minimal and noted
above.
