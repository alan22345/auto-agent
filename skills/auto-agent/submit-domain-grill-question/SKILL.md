---
name: submit-domain-grill-question
description: Domain-grill agent only — relay a clarifying question up to the user (or PO standin) about THIS domain's scope, constraints, or ambiguities. The orchestrator parks the scaffold parent in AWAITING_DOMAIN_GRILL until an answer lands, then re-invokes you with the answer in context.
---

<what-to-do>

Write the pending question to `.auto-agent/domain_grill_questions/<slug>.json` in the workspace, then stop.

`<slug>` is your domain's kebab-case slug — the orchestrator tells you which slug you own when it invokes you. Substitute the actual slug into the path literally — e.g. for the `auth` domain, the file path is `.auto-agent/domain_grill_questions/auth.json`.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "domain_slug": "<the same slug as in the path>",
  "question": "<the question to ask the user, framed so they can answer without needing to re-read the whole root ADR>"
}
```

Use Write (not Edit) to create the file — create the `.auto-agent/domain_grill_questions/` directory first if it doesn't exist.

</what-to-do>

<rules>

- This skill is for the domain-grill agent only. The domain architect uses `submit-domain-adr` to write its ADR.
- `schema_version` must be the string `"1"` literally.
- `domain_slug` in the JSON must match the slug in the filename. Use kebab-case throughout.
- The path is `.auto-agent/domain_grill_questions/<slug>.json` — replace `<slug>` with the actual slug, do not leave the placeholder in.
- Ask focused questions about THIS domain only. Cross-domain scope is the root architect's job; we are past that.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop. Your process will exit; the orchestrator surfaces the question to the user, persists their answer at `.auto-agent/domain_grill_answers/<slug>.json`, then re-invokes you so you can continue the grill or call `submit-domain-grill-summary`.

</rules>
