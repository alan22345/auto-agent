---
name: submit-plan
description: Persist the complex-flow plan so the orchestrator can present it to the user (or freeform standin) for approval. Use after you've reasoned about the task and are ready to commit to a concrete plan.
---

<what-to-do>

Write the complex-flow plan to `.auto-agent/plan.md` in the workspace, then stop.

The file is markdown. Use whatever section headings make the plan readable — at minimum it should include:

- Goal — one or two sentences on what the task accomplishes.
- Acceptance criteria — testable conditions that must be true when done.
- Files to modify — one line per file with a note on what changes.
- Steps — numbered list of what you will do, in order.

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- Do not output the plan in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/plan.md` after your turn returns; that is the only signal it needs from you here.

</rules>
