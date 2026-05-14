---
name: submit-grill-answer
description: Parent architect only — answer a grill question raised by one of your sub-architects. Use when the orchestrator hands you a slice's question; the sub-architect resumes once you've written the answer.
---

<what-to-do>

Write the answer to `.auto-agent/slices/<name>/grill_answer.json` in the workspace, then stop.

`<name>` is the slice the question came from — the orchestrator tells you which slice when it resumes your session with the pending question. Substitute the actual slice name into the path literally — e.g. for slice `auth`, the file path is `.auto-agent/slices/auth/grill_answer.json`.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "answer": "<the answer to the sub-architect's question, with enough context that they can keep going>"
}
```

Use Write (not Edit) to create the file — create the `.auto-agent/slices/<name>/` directory first if it doesn't exist.

</what-to-do>

<rules>

- This skill is for the parent architect only. Sub-architects use `submit-grill-question` to ask.
- `schema_version` must be the string `"1"` literally.
- The path is `.auto-agent/slices/<name>/grill_answer.json` — replace `<name>` with the actual slice name, do not leave the placeholder in.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop. The orchestrator re-invokes the sub-architect with your answer once the file lands.

</rules>
