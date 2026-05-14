---
name: submit-pr-review
description: Persist the self-PR-review verdict so the orchestrator can address own comments, signal the user, or auto-merge. Use after you've read the PR as a teammate would — title, description, commit narrative, diff, CI signals.
---

<what-to-do>

Write the self-PR-review verdict to `.auto-agent/pr_review.json` in the workspace, then stop.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "verdict": "approved",
  "comments": []
}
```

or, when changes are required:

```json
{
  "schema_version": "1",
  "verdict": "changes_requested",
  "comments": [
    {
      "path": "src/foo.py",
      "line": 42,
      "comment": "<one or two sentences explaining what to change>"
    }
  ]
}
```

`verdict` is `"approved"` or `"changes_requested"`. `comments` is a list — empty when approved, populated when changes are requested. Each comment may include `path`, `line`, and `comment` fields (path and line are optional for general PR-level comments).

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/pr_review.json` after your turn returns; that is the only signal it needs from you here.

</rules>
