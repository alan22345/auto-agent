---
name: submit-design
description: Persist the complex_large architect's design doc so the orchestrator can present it to the user (or freeform standin) for approval. Use once the design is concrete enough to commit to — this is the single approval artefact for the whole run.
---

<what-to-do>

Write the architect's design to `.auto-agent/design.md` in the workspace, then stop.

The file is markdown. The design doc is the single approval artefact for the whole complex_large run, so it should cover:

- Goal — what the task accomplishes, in one paragraph.
- Architecture sketch — the modules, routes, and data shapes you're going to introduce or change.
- Slice rationale — how you plan to break this into backlog items, and why each slice is its own item.
- Affected routes — the surface area the verify primitives will exercise once each item ships.
- Risks — anything that could derail the plan and how you will mitigate it.

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- Do not output the design in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/design.md` after your turn returns; that is the only signal it needs from you here.

</rules>
