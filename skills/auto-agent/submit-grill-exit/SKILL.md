---
name: submit-grill-exit
description: Persist the grill-me exit summary so the orchestrator can advance past the intake gate. Use when grilling is complete and the task is understood well enough to proceed.
---

<what-to-do>

Write the grill exit summary to `.auto-agent/grill.json` in the workspace, then stop.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "summary": "<one-paragraph summary of the agreed scope and intent>",
  "qa": [
    {"question": "<question you asked>", "answer": "<answer you got>"}
  ]
}
```

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/grill.json` after your turn returns; that is the only signal it needs from you here.

</rules>
