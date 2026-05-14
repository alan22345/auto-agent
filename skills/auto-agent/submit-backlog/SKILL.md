---
name: submit-backlog
description: Persist the architect's backlog of items so the orchestrator can dispatch builders for each one. Use once the design is approved and you've sliced the work into a concrete, validated backlog.
---

<what-to-do>

Write the backlog to `.auto-agent/backlog.json` in the workspace, then stop.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "items": [
    {
      "title": "<short title for the slice>",
      "description": "<at least 80 words explaining the slice in detail>",
      "justification": "<why this is its own slice rather than folded in>",
      "affected_routes": ["/api/foo", "/bar"],
      "affected_files_estimate": 4
    }
  ]
}
```

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- Every item must have all five fields — the orchestrator's structural validator rejects items missing any of them.
- `affected_files_estimate` is an integer (no quotes).
- `affected_routes` is a list of strings — empty list `[]` if the slice does not touch a route.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/backlog.json` after your turn returns; that is the only signal it needs from you here.

</rules>
