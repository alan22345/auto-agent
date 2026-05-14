---
name: submit-final-review
description: Persist the complex_large final-reviewer verdict so the orchestrator can either open a PR or bounce gaps back to the architect. Use after you've exercised the union of all affected routes against the integrated diff.
---

<what-to-do>

Write the final-reviewer verdict to `.auto-agent/final_review.json` in the workspace, then stop.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "verdict": "passed",
  "gaps": []
}
```

or, when gaps are found:

```json
{
  "schema_version": "1",
  "verdict": "gaps_found",
  "gaps": [
    {
      "description": "<one paragraph on the gap>",
      "affected_routes": ["/api/foo"]
    }
  ]
}
```

`verdict` is `"passed"` or `"gaps_found"`. When `passed`, `gaps` is the empty list `[]`. When `gaps_found`, each entry must include `description` and `affected_routes` (a list of route strings — may be empty).

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/final_review.json` after your turn returns; that is the only signal it needs from you here.

</rules>
