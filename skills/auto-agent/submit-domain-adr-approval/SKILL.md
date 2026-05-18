---
name: submit-domain-adr-approval
description: Persist the human (or PO standin) verdict on one domain ADR so the orchestrator can decide whether to spawn that domain's child trio. Use once per domain after reading `.auto-agent/adrs/<index>-<slug>.md`.
---

<what-to-do>

Write the verdict to `.auto-agent/domain_adr_approvals/<slug>.json` in the workspace, then stop.

`<slug>` is the kebab-case slug from the root ADR's `domains:` block — the orchestrator tells you which slug when it invokes you. Substitute the literal slug into the file path — e.g. for slug `auth`, the path is `.auto-agent/domain_adr_approvals/auth.json`.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "verdict": "approved",
  "comments": "<rationale — especially required for revise/rejected>"
}
```

`verdict` is one of:

- `"approved"` — this domain ADR is solid; the orchestrator will spawn its child trio once every domain has a non-revise verdict.
- `"revise"` — the domain ADR needs another pass; the orchestrator re-runs the matching domain architect with your comments. Bounded at 3 revise rounds per domain.
- `"rejected"` — this domain is fundamentally wrong; no child trio will spawn for it.

Use Write (not Edit) to create the file — create the `.auto-agent/domain_adr_approvals/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- `verdict` must be one of `"approved"`, `"revise"`, or `"rejected"`. The orchestrator rejects anything else.
- `comments` is required for `revise` and `rejected`; for `approved` a one-line "lgtm" or empty string is fine.
- The path is `.auto-agent/domain_adr_approvals/<slug>.json` — replace `<slug>` with the actual domain slug, do not leave the placeholder in.
- One verdict per domain — one file per slug. Do not bundle multiple domains into one file.
- Do not output the JSON in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/domain_adr_approvals/<slug>.json` after your turn returns; that is the only signal it needs from you here.

</rules>
