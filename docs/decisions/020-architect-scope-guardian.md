# [ADR-020] Architect is the scope guardian

## Status

Accepted

## Context

Auto-agent's trio (architect → builders → reviewer → final reviewer) ships
PRs autonomously. The architect writes `design.md` during the design phase,
the builders implement items from the backlog, the reviewer checks each
item, the final reviewer + smoke step verify the integrated diff, and on
remaining gaps the architect re-enters via `run_gap_fix` to dispatch
fixes.

Every gate downstream of the architect asks a *code-quality* question
("does it run?", "does the diff implement the item?", "are there
deferred stubs?"). **No downstream gate asks the scope question** — "is
this work inside what the task was asked to do?" The architect alone
sits at the boundary between human intent (the task description) and
trio execution.

Task 28 ("prettier apartment", 2026-05-27) shipped PR #53 to
iot-apartment-simulator. The task scope was explicit: *"frontend/ — 3D
rendering of the apartment in the main Apartment tab and the Cameras
tab. **Not the Physics floor-plan view, not backend.**"* The first
eight builder items (T1–T8) followed the design and shipped PBR
materials, point lights, GLB budgeting, a temperature overlay, etc.
Then the final reviewer's smoke step booted the app, noticed pre-
existing `NotImplementedError` stubs in `CounterfactualWorld.fork_from`
/ `tick` (left over from the abandoned task 170 work), and reported a
"gap." The gap-fix architect responded by **dispatching G1–G5 to build
the entire counterfactual simulation subsystem** — three REST
endpoints, a WebSocket sibling stream, a new `CounterfactualSession`
type, a side-by-side React view, a discomfort chart, an integration
test suite, and a docs page. None of that was in `design.md`. The PR
landed with the apartment work *and* a wholesale new feature the user
never asked for.

The root cause is not the gap-fix prompt being too soft. The gap-fix
architect did exactly what its prompt said: "close the gap." The root
cause is that **no actor in the trio is responsible for keeping the
work focused on the task's declared scope**. We need to assign that
responsibility, and the architect is the only role positioned to hold
it:

- The architect owns `design.md` (the canonical scope artefact).
- The architect runs at every decision point that could expand the
  backlog (initial design, backlog emit, checkpoint, gap-fix, replan).
- The trio's other roles (builders, reviewers, smoke agents) take
  inputs (item descriptions, diffs) and have no visibility into
  task-level intent.

## Decision

The architect is the **scope guardian**. This is a permanent
project-level invariant baked into every architect-phase system prompt
(`ARCHITECT_DESIGN_SYSTEM`, `ARCHITECT_BACKLOG_EMIT_SYSTEM`,
`ARCHITECT_INITIAL_SYSTEM`, `ARCHITECT_CHECKPOINT_SYSTEM`), with the
following load-bearing rules:

1. **`design.md` is the contract.** It defines what is in scope for
   the entire trio run. Once written and approved (complex_large) or
   committed (other flows), it cannot be silently expanded by later
   architect turns. Adding scope requires an explicit user
   conversation (an `iterate` round, ADR-017) or a new task.

2. **Before any `dispatch_new` / `revise` / new-item emission, the
   architect MUST ask the scope question for each candidate item:**
   *Is this item inside `design.md`'s declared scope?*

3. **Out-of-scope work is `escalate`d, not dispatched.** When the
   final reviewer (or CI, or checkpoint diff) surfaces a "gap" that
   is actually an unrelated pre-existing bug, a broken module the
   design doesn't mention, or a feature the user never asked for, the
   architect emits
   `{"action": "escalate", "reason": "gap is out-of-scope of design.md: <one-line>"}`.
   The orchestrator routes escalations to the operator (non-freeform)
   or the improvement-agent standin (freeform). Unrelated work
   becomes a separate task, not a stowaway in this one.

4. **The PO and improvement-agent paths pick up unrelated work.**
   It is *not* the architect's job to fix the codebase's bug backlog
   inside one task. Bugs found while doing task X are surfaced as
   PO suggestions / improvement-agent inputs; they are not silently
   added to task X's PR.

5. **Children inherit scope from `design.md`.** The reviewer, smoke
   agent, and final reviewer all read `design.md` (already pinned via
   `pinned_context.py`). When they evaluate gaps, they must scope-
   filter the same way — gaps outside the design's footprint are
   diagnostics, not blocking gaps. This is the architect's
   instruction to downstream gates, encoded in their prompts.

## Consequences

**Easier:**
- Task scope holds across the trio's full lifecycle. A "prettier
  apartment" task ships apartment work, not a counterfactual
  simulation backend.
- Operators get a clear signal (`action="escalate"`) when the trio
  hits an unrelated problem, rather than discovering scope creep in
  the final PR diff.
- The architect's responsibilities are explicit and consistent
  across every phase — no special-casing gap-fix vs. checkpoint vs.
  initial.

**Harder:**
- Architect prompt budget grows by ~30 lines per phase. Acceptable
  given the alternative is shipping wrong work.
- Some legitimate "this fix is small and obviously belongs here"
  cases (e.g. fixing an import path the design.md doesn't enumerate)
  must now be justified as "in-scope by interpretation," which the
  LLM may occasionally over-decline. Soft-warn telemetry surfaces
  borderline cases for operator review rather than auto-blocking.
- Existing tasks built before this ADR may have under-specified
  `design.md`s; for those, the architect defaults to a narrower
  reading and escalates more often than it dispatches. This is the
  intended bias.

**Out of scope for this ADR:**
- The programmatic scope-fit validator (path overlap between item
  `affected_files` and `design.md` references) — a useful soft-warn
  layer but a separate, deferrable piece of work. The ADR's
  authority is the system prompt; the validator is an extra
  guardrail.
- Final-reviewer scope-awareness — instructing the final reviewer to
  scope-filter the gaps it reports is the right structural follow-up.
  Documented here as the next step; lands separately because it
  requires careful regression testing on the final-reviewer prompt.

## Implementation

Each architect-phase system prompt in
`agent/lifecycle/trio/prompts.py` gains a `**Scope guardian (ADR-020):**`
block referencing this ADR. The block sits alongside the existing
`No-defer rule` block — both are project-level invariants the
architect carries across all phases.
