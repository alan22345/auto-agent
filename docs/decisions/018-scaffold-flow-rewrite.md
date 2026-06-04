# [ADR-018] Scaffold flow rewrite — intent grill, ADR-driven decomposition, per-domain trios

> **Summary:** Scaffold = four-phase pipeline (intent grill → root architect → per-domain architects → per-domain trios) with a project-level verification gate after all domain trios finish.

## Status

Accepted

Supersedes the scaffold portion of ADR-015 (single design doc, complex_large pre-classification of `/freeform/create-repo` tasks). ADR-015 still governs the trio that runs per-domain. ADR-017 (trio iteration phase) applies unchanged to each domain's PR.

## Context

`POST /freeform/create-repo` today (`orchestrator/create_repo.py::create_repo_and_scaffold_task`) names a repo, creates it on GitHub, inserts `Repo` + `FreeformConfig`, and queues a single `Task` pre-classified as `COMPLEX_LARGE` with `freeform_mode=True`. That task runs through one trio: architect produces one `design.md`, dispatcher runs one backlog, one final review, one PR.

Three failures of this shape:

1. **No intent grill.** The classifier never runs (complexity is pre-set), so `needs_grill` is never evaluated and `intake_qa` stays `None`. Whether the user is grilled at all depends on the architect happening to ask a clarification — not the same as a gate. See `feedback-always-grill`: grill is mandatory on every flow, scaffold included.
2. **One design doc for an entire new product.** "Build a SaaS app" produces one architect pass that has to reason about auth, billing, data model, UI, deployment in the same context window. Output is shallow, items don't slice cleanly, the design-approval gate gives the user one accept/reject button on an artefact too large to review.
3. **No project-level verification gate.** Per-item reviewer (ADR-015 §3) and final reviewer (ADR-015 §4) run smoke+UI within one trio. When the project is many trios stitched together over many PRs, nothing exercises the integrated whole.

## Decision

**Replace the single-trio scaffold flow with a four-phase pipeline: intent grill → root architect → per-domain architects → per-domain trios. Add a project-level verification gate after all domain trios finish.**

### 1. New parent task shape

`/freeform/create-repo` creates a **scaffold parent** Task:

- New complexity bucket `TaskComplexity.SCAFFOLD` in `shared/models.py` (not a `complex_large` overload).
- Status starts at `AWAITING_INTENT_GRILL`.
- `freeform_mode=True` retained — PO agent stands in at every gate in freeform mode.
- The scaffold parent does **not** itself run a trio. It orchestrates child tasks that do.

Routing in `run.py:on_task_classified`: when `complexity == SCAFFOLD`, dispatch `agent.lifecycle.scaffold.run_scaffold_parent` instead of either the legacy planning path or `run_trio_parent`.

### 2. Phase A — intent grill

Mandatory gate, never skippable (consistent with `feedback-always-grill`). Runs before any architect agent.

- Driven by an agent that grills the user against the raw description.
- Output: `.auto-agent/intent.md` — pinned statement of what the user wants. This becomes the canonical input to every architect downstream.
- Submitted via a new `submit-intent-summary` skill (extends the skills-bridge contract from ADR-015 §12).
- In freeform mode, `agent/po_agent.py::po_answer_intent_grill` provides the standin answers, sourced from the repo's `product_brief` (will be empty for a brand-new scaffold, so the PO falls back to defaults logged with `fallback_default(source=heuristic)`).

State transition: `AWAITING_INTENT_GRILL → INTENT_GRILL_DONE → BUILDING_ROOT_ADR`.

### 3. Phase B — root architect

One architect agent produces the system-level ADR.

- Reads `.auto-agent/intent.md`.
- Writes `.auto-agent/adrs/000-system.md` via a new `submit-root-adr` skill. Required structure (validated by `agent/lifecycle/scaffold/validators.py`):
  - Vision (1–2 paragraphs).
  - Bounded contexts / domains, each with a one-paragraph scope, public surface, dependencies on other domains.
  - Cross-cutting concerns (auth, observability, deployment, data layer).
  - Named domain list for Phase C: `domains: [{name, scope_summary}, ...]`.
- **Bounded at ≤10 domains.** If the root architect emits >10 it's rejected with feedback and re-tried in the same session (architect's persisted session model from ADR-015 §13). After 2 rejections, escalate.

State transition: `BUILDING_ROOT_ADR → AWAITING_ROOT_ADR_APPROVAL`.

### 4. Phase B-gate — root ADR grill

Single gate on the root ADR.

- User (or PO standin) reviews the doc in a dedicated web-next pane.
- Verdict shape `.auto-agent/root_adr_approval.json`: `{verdict: "approved" | "revise" | "rejected", comments: str}`.
- On `revise`, root architect's session resumes with the comments prepended; bounded to 3 revise rounds, then escalate.
- On `rejected`, the scaffold parent transitions to `BLOCKED`.

State transition: `AWAITING_ROOT_ADR_APPROVAL → BUILDING_DOMAIN_ADRS`.

### 5. Phase C — per-domain architects (serial)

One architect per named domain, run **serially** (so each architect's session can ask the root architect for clarification via the skill-bridge relay from ADR-015 §10).

For each domain, **before** the architect writes its ADR, a per-domain grill round runs (added in Stage 8 under the always-grill principle — feedback-always-grill):

1. **Domain grill** (`domain_grill.run`) — reads `intent.md` + `000-system.md` + the domain entry, then drives a focused grill conversation about THIS domain's scope, boundary lines, deliberate non-goals, and constraints. Writes `.auto-agent/adrs/<n>-<slug>.grill.md` via `submit-domain-grill-summary`. When it needs to ask the user something, it calls `submit-domain-grill-question` (file at `.auto-agent/domain_grill_questions/<slug>.json`), the SCAFFOLD parent transitions to `AWAITING_DOMAIN_GRILL`, and the parent driver returns. The router's POST `/scaffold/domain-grill-answer` endpoint persists the answer to `.auto-agent/domain_grill_answers/<slug>.json`, transitions back to `BUILDING_DOMAIN_ADRS`, and re-invokes the driver. In freeform mode the PO standin (`po_answer_domain_grill`) answers instead.
2. **Domain architect** — reads `intent.md` + `000-system.md` + **the grill summary** (`adrs/<n>-<slug>.grill.md`, treated as authoritative for this domain) + the domain entry. Writes `.auto-agent/adrs/<n>-<domain-slug>.md` via `submit-domain-adr`. Required structure:
   - DDD-flavoured scope: aggregates, ubiquitous language, invariants.
   - Public surface (routes, events, public types).
   - Integration points with other domains.
   - Affected routes for verification (consumed later by ADR-015 §11 verify primitives).
   - Same length/structural validators as ADR-015 §9 (≥80 word descriptions, justification field, affected_routes list).

Per-domain progress (which domain index the loop is currently on) is persisted on `task.subtasks["scaffold"]["current_domain_idx"]` so re-entry after a grill pause resumes on the right domain.

State transitions: `BUILDING_DOMAIN_ADRS → AWAITING_DOMAIN_GRILL` (grill paused) → `BUILDING_DOMAIN_ADRS` (user answered) → ... → `AWAITING_DOMAIN_ADR_APPROVAL` (all domains done).

### 6. Phase C-gate — per-domain ADR grill (serial gates, no bundling)

Each domain ADR gets its own gate. No "approve all" shortcut — the user can approve one and revise another in any order. Web-next surfaces them as a list with per-row verdict.

- Verdict file per ADR: `.auto-agent/domain_adr_approvals/<slug>.json`.
- `revise` re-enters the matching domain architect's session; 3-round cap then escalate.
- `rejected` removes the domain from the build set (parent doesn't fail — user can re-scope by editing 000-system.md and re-running Phase C for the missing slice; explicit user action required, no auto-recovery).

State transition advances only when **every** domain ADR has a non-`revise` verdict: `AWAITING_DOMAIN_ADR_APPROVAL → DISPATCHING_DOMAIN_BUILDS`.

### 7. Phase D — per-domain trios

For each approved domain ADR, spawn a child `Task`:

- `parent_task_id = <scaffold parent>`.
- `complexity = COMPLEX_LARGE`.
- `freeform_mode` inherited from parent.
- `design.md` for the child is the domain ADR (symlink or copy into `.auto-agent/design.md` inside the child's workspace).

Children run the existing trio flow (architect → backlog → dispatcher → reviewer → final review → PR) **unchanged**. ADR-015 §11 verify primitives (smoke + UI + diff-stub grep) apply per-item and at child final review. ADR-017 iteration loop applies to each child's PR.

**Trio's existing sub-architect spawn mechanism (ADR-015 §9) is inherited.** If a domain ADR is too big for one trio's architect to slice, that architect emits `decision=spawn_sub_architects` and the sub-architects run inside the trio as today. This is trio functionality, not scaffold functionality — we don't reinvent it.

**Concurrency:** children respect the existing 2-slot system (`run.py:on_task_classified`). Domain trios queue and drain.

**Children open separate PRs** — one per domain, each merging to the repo's default branch (no shared integration branch). This keeps each PR human-reviewable on its own and lets verify primitives target one domain at a time.

State transition: `DISPATCHING_DOMAIN_BUILDS → BUILDING_DOMAINS → AWAITING_FINAL_VERIFICATION` (latter fires once every child Task is in a terminal state).

### 8. Phase E — project-level final verification

After all children are DONE (or FAILED — FAILED children block this gate), a project-level final-reviewer agent runs verify primitives across the integrated whole.

- Reads `intent.md` + `000-system.md` + every domain ADR + the union of `affected_routes` from every child final-review summary.
- Runs `verify_primitives.boot_dev_server()` → `exercise_routes(union_of_all_routes)` → `inspect_ui(route, intent)` for each UI-touching route → `grep_diff_for_stubs(diff_of_all_child_PRs_merged)`.
- Writes `.auto-agent/scaffold_final_verification.json` via `submit-scaffold-final-verification`.
- On `gaps_found`, the gap list is converted into one or more new child Tasks (same `complexity=COMPLEX_LARGE`, `parent_task_id=<scaffold parent>`) that go through Phase D. Bounded at 3 project-level rounds.

State transition: `AWAITING_FINAL_VERIFICATION → DONE` on pass; `→ DISPATCHING_DOMAIN_BUILDS` on `gaps_found` (with new gap-fix children); `→ BLOCKED` after 3 rounds.

### 9. Workspace layout extension

Adds to ADR-015 §12:

```
.auto-agent/
├─ intent.md                              # Phase A output
├─ adrs/
│   ├─ 000-system.md                      # Phase B output
│   ├─ 001-<domain>.md                    # Phase C outputs
│   └─ ...
├─ root_adr_approval.json                 # Phase B-gate
├─ domain_adr_approvals/
│   └─ <slug>.json                        # Phase C-gate
├─ scaffold_final_verification.json       # Phase E
└─ (existing trio paths per child task)
```

### 10. State machine additions

In `orchestrator/state_machine.py`, new statuses:

- `AWAITING_INTENT_GRILL`
- `BUILDING_ROOT_ADR`
- `AWAITING_ROOT_ADR_APPROVAL`
- `BUILDING_DOMAIN_ADRS`
- `AWAITING_DOMAIN_GRILL` (Stage 8) — parent parked while the per-domain grill agent waits for the user's answer
- `AWAITING_DOMAIN_ADR_APPROVAL`
- `DISPATCHING_DOMAIN_BUILDS`
- `BUILDING_DOMAINS`
- `AWAITING_FINAL_VERIFICATION`

These only apply to `TaskComplexity.SCAFFOLD` parents; trio-internal statuses on children are unchanged.

### 11. Skills added

All follow the ADR-015 §12 skills-bridge contract (CC writes JSON/MD to a known path; orchestrator reads after `agent.run` returns):

- `submit-intent-summary` → `.auto-agent/intent.md`
- `submit-root-adr` → `.auto-agent/adrs/000-system.md`
- `submit-domain-grill-question` (Stage 8) → `.auto-agent/domain_grill_questions/<slug>.json` (grill agent → user relay)
- `submit-domain-grill-summary` (Stage 8) → `.auto-agent/adrs/<n>-<slug>.grill.md` (authoritative grill output for the per-domain architect)
- `submit-domain-adr` → `.auto-agent/adrs/<n>-<slug>.md`
- `submit-root-adr-approval` → `.auto-agent/root_adr_approval.json`
- `submit-domain-adr-approval` → `.auto-agent/domain_adr_approvals/<slug>.json`
- `submit-scaffold-final-verification` → `.auto-agent/scaffold_final_verification.json`

### 12. PO standin extensions

`agent/po_agent.py` gains:

- `po_answer_intent_grill(task, question, workspace_root)`
- `po_approve_root_adr(task, adr_md, workspace_root)`
- `po_approve_domain_adr(task, adr_md, workspace_root)`

Standins log every gate decision with cited context (PO output for new scaffolds is heuristic — there's no product brief yet — and that's surfaced in the UI gate history).

## Consequences

**Easier:**

- Each architect agent has a tight context (one domain, not "the whole product").
- The user reviews ADRs incrementally instead of one giant design doc.
- Each domain is independently PR-able and revertable.
- Final verification catches integration gaps that today's per-trio gates can't see.
- The intent grill makes the project's "why" first-class and citable from every downstream agent.

**Harder:**

- More state. The scaffold parent has 8 new statuses and many child tasks; UI and `run.py` event handlers carry more shape.
- Latency. Five sequential phases (intent grill → root ADR → N domain ADRs → N trios → final verification) on a new repo is many serial agent runs. Acceptable for "build me a SaaS app"; over-engineered for "build me a CLI that prints fizzbuzz" — see open question.
- Failure modes proliferate. A revise loop at any of three grill points can stall the parent. The 3-round caps + escalate-to-blocked policy keeps this bounded but the UX for "your scaffold is blocked at root-ADR-revise round 3" is new work.
- Project-level verify needs the whole repo to boot, which depends on the onboarding harness having produced a working `auto-agent.smoke.yml` — itself a child of one of the domain trios. Bootstrap ordering: the harness ADR/domain must be the first child trio.

**Open questions (resolve before implementation):**

- Should `TaskComplexity.SCAFFOLD` be exposed as a UI choice ("build something new" vs "add a feature"), or is it implicit from the `/freeform/create-repo` endpoint? Current proposal: implicit — only the endpoint creates a scaffold parent.
- Does the project-level final verification get a human approval gate (in human-in-loop mode), or is its verdict purely automated? Current proposal: automated verdict feeds into the existing per-task PR-review gate of the last child; no extra human gate.
