# Architecture Decision Index

_Active decisions only. Superseded/Deprecated ADRs are intentionally omitted — read the file's `## Status` before treating any ADR as binding._

- ADR-001 Add Harness Engineering Infrastructure — Bootstrap the harness — CLAUDE.md, ruff lint, pre-commit hooks, docs/decisions/, and entropy.yml idle checks — so agents and humans share enforced guardrails.
- ADR-002 Memory browser uses the existing WebSocket contract for read + write — Memory-tab read+write rides the existing WebSocket contract behind one shared/memory_io.py seam; fact deletion is a soft delete that preserves the audit trail.
- ADR-003 Vendor Matt Pocock's Engineering Skills + Grill Loop + Architecture Mode — Vendor the Pocock engineering skills into skills/engineering/, bake the architecture lens into the base prompt, and grill BEFORE planning via a persisted intake_qa round-trip.
- ADR-004 Run Alembic Migrations at Lifespan Startup — Run `alembic upgrade head` automatically at FastAPI startup (before create_all), wrapped so a bad migration logs and still boots on the prior consistent schema.
- ADR-005 Workspace path resolution as a single tool seam — All file tools resolve paths through one ToolContext.resolve() seam owning the sandbox invariant — kills five divergent copies and the /work→/workshop escape bug.
- ADR-006 Extract Anthropic message mapper as the LLM provider seam — agent/llm/anthropic_mapper.py is the single owner of Message↔Anthropic-API translation; Bedrock and Anthropic providers shrink to thin transport+auth adapters.
- ADR-007 Event publishing as a real seam — shared/events.py is the single publish seam (Publisher protocol + Redis/in-memory adapters); call sites just publish() with no Redis knowledge.
- ADR-008 Collapse `claude_runner/` into `agent/` — Apply the deletion test — delete claude_runner/ entirely and reroute its one live path through the LLMProvider seam.
- ADR-009 Split `agent/main.py` into per-phase task lifecycle modules — Split the monolithic agent/main.py into per-phase modules under agent/lifecycle/, each owning one phase's handler, dispatched through the EventBus.
- ADR-010 Single owner of "LLM text reply → dict" — agent/llm/structured.py is the single owner of "LLM text reply → dict" (parse_json_response + complete_json with bounded retry); callers pick the fallback policy.
- ADR-011 Typed event taxonomy in shared/events.py — Typed event taxonomy in shared/events.py — StrEnum types + one factory per event so a payload typo fails at the producer, wire string unchanged.
- ADR-012 LGTM-driven auto-merge and merge-conflict resolution — Reviewer-approved (LGTM) freeform PRs auto-merge when CI is green and conflict-free; dirty PRs trigger the conflict resolver. Non-freeform still needs human review.
- ADR-014 Split the trio decision contract — prose by the architect, structure by a cheap classifier — Split the trio decision contract — heavy model reasons in prose, a cheap Bedrock-Haiku extractor turns prose into the structured JSON envelope, independent of LLM_PROVIDER.
- ADR-015 Task flow redesign — three classifications, freeform mode, no-defer enforcement — Three flows (simple/complex/complex_large) on one classifier with conditional grill, a single design.md approval artefact, heavy per-item review, and four-layer no-defer enforcement.
- ADR-016 Code graph — hierarchical, function-level, cited-edge map of an onboarded repo — Opt-in hierarchical, function-level, citation-validated code graph — one canonical store served to humans as a visualisation and to the agent through a typed query tool.
- ADR-017 Trio iteration phase — post-PR feedback loop — Add a long-lived AWAITING_REVIEW phase + ITERATING sub-state for post-PR feedback; PR_CREATED becomes a one-shot transit event, not a resting status.
- ADR-018 Scaffold flow rewrite — intent grill, ADR-driven decomposition, per-domain trios — Scaffold = four-phase pipeline (intent grill → root architect → per-domain architects → per-domain trios) with a project-level verification gate after all domain trios finish.
- ADR-019 Per-repo project secrets vault — architect-declared, user-managed, build-gating — Per-repo encrypted secret vault — architect declares a required-secrets manifest, a hard gate blocks the build until they're populated, injected into the project runtime at boot.
- ADR-021 Subprocess execution as a single seam — agent/sh.py is the single subprocess seam — owns env-merge (GIT_TERMINAL_PROMPT=0), timeout-with-kill, and output capping for every agent shell-out.
- ADR-022 Per-task Redis state as a real seam (TaskChannel) — shared/task_channel.py is the per-task Redis seam (TaskChannel protocol: guidance/heartbeat/stream verbs), mirroring the Publisher shape.
- ADR-023 Code graph as the agent's navigation substrate — search_symbols + get_symbol_source close the graph's navigation loop, a per-task stdio MCP server bridges it to the claude_cli path, and search-before-new-helper becomes the stated dedup convention.
