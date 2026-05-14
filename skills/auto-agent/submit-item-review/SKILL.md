---
name: submit-item-review
description: Persist the per-item heavy-reviewer verdict — alignment, smoke, and UI — so the orchestrator can advance the dispatcher loop. Use after you've reviewed one backlog item's diff against its spec and exercised its affected routes.
---

<what-to-do>

Write your per-item verdict to `.auto-agent/reviews/<item_id>.json` in the workspace, then stop.

`<item_id>` is the backlog item id the orchestrator told you to review (you'll find it in the prompt). Substitute it literally — e.g. for item `T3`, the file path is `.auto-agent/reviews/T3.json`.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "verdict": "pass",
  "alignment": "<one sentence on how the diff matches the item spec>",
  "smoke": "<one sentence on the verify_primitives.exercise_routes outcome>",
  "ui": "<one sentence on the verify_primitives.inspect_ui outcome, or 'n/a' if no UI routes>",
  "reason": "<one paragraph synthesising the verdict>"
}
```

`verdict` is `"pass"` or `"fail"`.

Use Write (not Edit) to create the file — create the `.auto-agent/reviews/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- The path is `.auto-agent/reviews/<item_id>.json` — replace `<item_id>` with the actual backlog item id, do not leave the placeholder in.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/reviews/<item_id>.json` after your turn returns; that is the only signal it needs from you here.

</rules>
