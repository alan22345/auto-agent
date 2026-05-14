---
name: submit-grill-question
description: Sub-architect only — relay a grill question up to the parent architect. Use when you need a design clarification that only the parent has the context to answer. The parent answers via submit-grill-answer.
---

<what-to-do>

Write the question to `.auto-agent/slices/<name>/grill_question.json` in the workspace, then stop.

`<name>` is your slice name — the orchestrator tells you which slice you own when it invokes you. Substitute the actual slice name into the path literally — e.g. for slice `auth`, the file path is `.auto-agent/slices/auth/grill_question.json`.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "question": "<the question to ask the parent architect, framed clearly enough that they can answer without further context>"
}
```

Use Write (not Edit) to create the file — create the `.auto-agent/slices/<name>/` directory first if it doesn't exist.

</what-to-do>

<rules>

- This skill is for sub-architects only. The parent architect uses `submit-grill-answer` to reply.
- `schema_version` must be the string `"1"` literally.
- The path is `.auto-agent/slices/<name>/grill_question.json` — replace `<name>` with the actual slice name, do not leave the placeholder in.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop. Your process will exit; the orchestrator picks up the file, asks the parent, then re-invokes you with the answer.

</rules>
