---
name: submit-scaffold-final-verification
description: Persist the project-level final-verification verdict for an ADR-018 scaffold run so the orchestrator can either transition the scaffold parent to DONE or spawn gap-fix child tasks. Use after you've exercised the union of all domain affected_routes against the integrated diff.
---

<what-to-do>

Write the verdict to `.auto-agent/scaffold_final_verification.json` in the workspace, then stop.

The file must be valid JSON. When the integrated system ships what the user asked for:

```json
{
  "schema_version": "1",
  "verdict": "passed",
  "comments": "<one-paragraph summary of the signals you collected>"
}
```

When there are gaps:

```json
{
  "schema_version": "1",
  "verdict": "gaps_found",
  "comments": "<one-paragraph summary>",
  "gaps": [
    {
      "description": "<what's missing or broken>",
      "affected_routes": ["/api/..."]
    }
  ]
}
```

`verdict` is `"passed"` or `"gaps_found"`. When `passed`, the `gaps` array should be absent or `[]`. When `gaps_found`, each entry must include `description`; `affected_routes` is a list of route strings (may be empty when the gap is not route-shaped — e.g. a boot failure).

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- `verdict` must be one of `"passed"` or `"gaps_found"`. The orchestrator rejects anything else.
- `gaps` is required (and non-empty) only when `verdict == "gaps_found"`. For `passed` it may be absent or the empty list `[]`.
- Each gap entry needs at least `description`. `affected_routes` is optional but strongly preferred so the gap-fix children know which routes to re-verify.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/scaffold_final_verification.json` after your turn returns; that is the only signal it needs from you here.

</rules>
