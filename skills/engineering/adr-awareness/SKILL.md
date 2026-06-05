---
name: adr-awareness
description: Use when about to write, change, or commit code in this repo, before overwriting or replacing existing functionality, and before opening a PR. Also when two ADRs contradict each other or an ADR describes behavior the code no longer has.
---

# ADR Awareness

This repo records Architecture Decision Records in `docs/decisions/`. They are binding context AND a liability when stale. Two duties on every code change:

## Duty 1 — Consult before you change
1. Read `docs/decisions/INDEX.md` — the active decisions, one line each.
2. Read the full body of any ADR whose summary touches your change.
3. Only `Accepted` / `Proposed` ADRs are binding. INDEX.md already omits Superseded/Deprecated; if you open a file directly, check its `## Status`.
4. Follow the binding ADR, or state your deviation explicitly and record a new ADR.

## Duty 2 — Retire when you overwrite
When your change replaces or removes functionality an ADR describes, retire that ADR **in the same commit**:
1. Edit only its `## Status`: `Superseded by [ADR-NNN] — <one line>` (or `Deprecated — <one line>` if nothing replaces it).
2. Keep the file — never delete it. The rationale is the history.
3. Regenerate the index: `.venv/bin/python3 -c "from agent.context.adr_index import build_index; open('docs/decisions/INDEX.md','w').write(build_index('docs/decisions'))"`
4. Commit the retirement with the code change, not separately.

## Red flags — STOP
- "I'll update the ADR later / a teammate owns docs." → The contradiction ships in the gap. Retire it here, or STOP and escalate.
- "I'll just delete the old ADR." → No. Soft-retire (set Status), keep the file.
- "This ADR looks outdated, I'll ignore it." → If its Status is `Accepted`, it is binding until you retire it.
