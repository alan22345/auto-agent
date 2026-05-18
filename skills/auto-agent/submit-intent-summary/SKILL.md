---
name: submit-intent-summary
description: Persist the intent-grill summary so the orchestrator can advance past the intent gate. Use once you've grilled the user (or the freeform PO standin) and have a sharp answer for purpose, users, must-haves, non-goals, constraints, and success criteria.
---

<what-to-do>

Write the intent summary to `.auto-agent/intent.md` in the workspace, then stop.

The file is markdown. It is the canonical statement of "what the user wants" that every downstream architect (root, per-domain, final verifier) will read. Use this exact section shape so downstream prompts can locate each piece:

```markdown
# Intent

## What the user wants
<2-3 paragraphs summarizing the scope, goal, and constraints captured during the grill round>

## Out of scope
- <bullet list of things the user explicitly does NOT want>

## Open questions for downstream architects
- <bullet list — these get passed into the root-architect prompt as hints>
```

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- All three sections (`## What the user wants`, `## Out of scope`, `## Open questions for downstream architects`) MUST appear by name; downstream prompts grep for them.
- `## What the user wants` is prose (2-3 paragraphs), not a bullet list.
- `## Out of scope` and `## Open questions for downstream architects` are bullet lists. Empty list is OK — write `- (none)` rather than omitting the section.
- Do not output the intent in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/intent.md` after your turn returns; that is the only signal it needs from you here.

</rules>
