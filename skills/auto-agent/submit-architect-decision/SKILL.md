---
name: submit-architect-decision
description: Persist the architect's per-cycle decision so the orchestrator knows what to do next. Use at every architect checkpoint — done, dispatch new items, escalate, spawn sub-architects, or await clarification.
---

<what-to-do>

Write the architect's decision to `.auto-agent/decision.json` in the workspace, then stop.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "action": "done",
  "payload": {}
}
```

`action` is one of:

- `"done"` — every backlog item has shipped and the integrated diff passes the final review. Payload may be `{}`.
- `"dispatch_new"` — close gaps in the existing work by appending fresh backlog items. Payload: `{"items": [...]}` with the same item shape as `submit-backlog`.
- `"escalate"` — auto-agent cannot close the loop on its own; raise to the human (or freeform standin). Payload: `{"reason": "..."}`.
- `"spawn_sub_architects"` — the task is huge enough to warrant slicing across sub-architects. Payload: `{"slices": [{"name": "auth", "scope": "..."}, ...]}`.
- `"awaiting_clarification"` — a question for the user blocks further progress. Payload: `{"question": "..."}`.

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- `action` must be one of the five values above. The orchestrator rejects anything else.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/decision.json` after your turn returns; that is the only signal it needs from you here.

</rules>
