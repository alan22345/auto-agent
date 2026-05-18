---
name: submit-domain-grill-summary
description: Persist the per-domain grill summary so the domain architect for THIS domain has a sharp, user-grounded statement of what to put in the domain ADR. Use once the grill round for the domain is complete and you have answers (from the user or PO standin) for every open question that mattered.
---

<what-to-do>

Write the grill summary to `.auto-agent/adrs/<NNN>-<slug>.grill.md` in the workspace, then stop.

`<NNN>` is the zero-padded domain index from the root ADR (e.g. `001`, `002`) and `<slug>` is the kebab-case slug — the orchestrator tells you both when it invokes you. For example, for the `auth` domain at index 1 the file path is `.auto-agent/adrs/001-auth.grill.md`.

The file is markdown. It is consumed by the domain architect for this domain as authoritative context (alongside `intent.md` and `000-system.md`). Use this exact section shape so the architect's prompt can grep for each piece:

```markdown
# Domain grill — <Domain name>

## Scope
<2-3 paragraphs summarising what this domain owns, with the boundary lines that came out of the grill made explicit>

## Open questions answered
- Q: <question you asked>
  A: <answer you got>
- ...

## Out of scope for this domain
- <bullet list — things explicitly NOT in this domain, often "belongs to <other-domain>">

## Constraints surfaced
- <bullet list — stack/data/integration constraints the user named during the grill that the architect must respect>
```

Use Write (not Edit) to create the file — create the `.auto-agent/adrs/` directory first if it doesn't exist.

</what-to-do>

<rules>

- All four sections (`## Scope`, `## Open questions answered`, `## Out of scope for this domain`, `## Constraints surfaced`) MUST appear by name. Empty section is OK — write `- (none)` rather than omitting the header.
- `## Scope` is prose, not a bullet list.
- The grill summary is per-domain — focus on this domain only. Cross-domain decisions belong in the root ADR (already written) and are out of scope here.
- Do not output the summary in the chat — only write the file.
- Do not perform any other actions in this turn. Just write the file and stop.
- The orchestrator reads `.auto-agent/adrs/<NNN>-<slug>.grill.md` after your turn returns; that is the only signal it needs from you here.

</rules>
