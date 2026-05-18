---
name: submit-root-adr-approval
description: Persist the human (or PO standin) verdict on the root ADR so the orchestrator can transition the scaffold parent — approved fans out to per-domain architects, revise loops back to the root architect, rejected blocks. Use once you've read `.auto-agent/adrs/000-system.md` and decided.
---

<what-to-do>

Write the verdict to `.auto-agent/root_adr_approval.json` in the workspace, then stop.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "verdict": "approved",
  "comments": "<rationale — especially required for revise/rejected>"
}
```

`verdict` is one of:

- `"approved"` — the root ADR captures the right system decomposition; the orchestrator fans out per-domain architects.
- `"revise"` — the root ADR needs another pass; the orchestrator re-runs the root architect with your comments. Bounded at 3 revise rounds.
- `"rejected"` — the root ADR is fundamentally wrong; the orchestrator blocks the scaffold parent for human intervention.

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- `verdict` must be one of `"approved"`, `"revise"`, or `"rejected"`. The orchestrator rejects anything else.
- `comments` is required for `revise` and `rejected` — the root architect needs the rationale to fix what's wrong. For `approved` a one-line "lgtm" or empty string is fine.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/root_adr_approval.json` after your turn returns; that is the only signal it needs from you here.

</rules>
