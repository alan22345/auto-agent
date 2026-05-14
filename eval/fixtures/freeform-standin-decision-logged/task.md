# Task — Freeform standin decision logged (ADR-015 §6 + Phase 12)

A moderately complex task in a freeform-mode repo (or with
`mode_override="freeform"`). The architect must emit a grill question
so the standin (PO by default for user-created tasks) is invoked. The
standin must:

  1. Write `.auto-agent/grill_answer.json`.
  2. Publish a `standin.decision` Redis event with the full payload.
  3. Persist a `gate_decisions` row with the full audit schema.

> Add a "share trip" feature to the travel-planning app: a button on
> the itinerary view opens a modal showing a shareable URL and a copy-
> to-clipboard helper; viewing the URL while logged-out shows the
> itinerary read-only. Persist a `share_token` against the itinerary
> row.

## Expected behaviour

The architect grills for "should the share token be revocable, and
should viewers be able to see the trip author's name". The PO standin
answers using the product brief, writes the gate file, publishes the
event, and the orchestrator persists a `gate_decisions` row whose
`source = "po_standin"` and whose `verdict` matches the answer.

## Pass criterion

All three sinks (file, event, DB row) are present, agree on the
decision, and the audit row carries all required fields
(`standin_kind`, `agent_id`, `gate`, `decision`, `cited_context`,
`fallback_reasons`, `timestamp`).
