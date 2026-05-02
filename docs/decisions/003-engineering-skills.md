[ADR-003] Vendor Matt Pocock's Engineering Skills + Grill Loop + Architecture Mode

## Status

Accepted

## Context

Auto-agent's `agent/tools/skill.py` has long supported loading "skill" .md
files from the `superpowers/` git submodule (obra/superpowers). The
practical complaint with that setup was twofold:

1. **No interactive design alignment.** The agent goes from task → planning
   → implementation with no enforced moment to align with the user on
   domain language, seam placement, or what "done" looks like. Plans are
   produced from the task description alone; misalignment is only caught
   at PR review, by which point the code is already wrong.

2. **No architectural lens during coding.** The system prompt describes
   tooling and ROOT-CAUSE rules, but doesn't tell the agent how to *judge*
   whether a new module earns its keep. The result has been: too many
   shallow `FooBarHandler`-style modules, pass-through layers, and
   premature ports introduced for a single test caller.

Matt Pocock's engineering skills repo
(<https://github.com/mattpocock/skills/tree/main/skills/engineering>)
ships ready-made `.md` skills that target both gaps: `grill-with-docs`
(interactive Q&A loop), `improve-codebase-architecture` (depth/seam/locality
vocabulary plus the deletion test), `tdd`, `diagnose`, `zoom-out`,
`triage`, `to-prd`, `to-issues`. They are designed to be loaded by an
agent loop on demand.

The user wanted three concrete behaviours, not just availability:

- **Grill BEFORE planning, not when reviewing the plan.** Multi-round
  interactive Q&A using the existing `awaiting_clarification` state.
- **Architecture lens enforced in CODING**, not just discussed in plans —
  every module judged through depth/seam/leverage/locality.
- **A new "Architecture Mode" parallel to Freeform Mode** — periodic,
  per-repo, runs `improve-codebase-architecture` on a cron and produces
  auto-approvable deepening tasks.

## Decision

### 1. Vendor (don't submodule) the engineering skills

The Pocock skills are copied verbatim into `skills/engineering/`. We
considered adding them as another submodule, but:

- Skills are small (~20 .md files, all text). Pinning a submodule SHA adds
  CI complexity (init/update) for no real space savings.
- Two universal rewrites are needed for them to work in this repo
  (`docs/adr/` → `docs/decisions/`; "the Agent tool" → "the `subagent`
  tool"). A vendored copy lets us own those rewrites without forking
  upstream.
- The skills are stable reference material, not actively iterated upstream.

`agent/tools/skill.py` was extended to discover skills from BOTH
`skills/engineering/` (first) and `superpowers/skills/` (second). On a
name collision the engineering version wins.

### 2. Grill BEFORE planning — multi-round, interactive

A new `tasks.intake_qa` JSONB column persists the grill transcript
(`list[{question, answer}]`) across the existing `PLANNING ↔
AWAITING_CLARIFICATION` round-trip. Three states:

- `intake_qa is None` — grilling not yet started.
- `intake_qa = []` — grilling complete, or skipped (simple tasks,
  architecture-suggestion tasks).
- `intake_qa = [{question, answer}, …]` — in-progress or completed.

`agent/main.py::handle_planning` gates: complex tasks with `intake_qa is
None` enter the grill flow (`build_grill_phase_prompt`), emit one
`CLARIFICATION_NEEDED:` per turn, and exit when the agent emits
`GRILL_DONE: <reason>`. `handle_clarification_response` detects grill
replies (intake_qa has a trailing `{question, answer: None}` entry) and
re-enters the grill loop instead of the generic resume path.

We considered extracting a `GrillCoordinator` module but the loop is one
read-modify-write cycle on `intake_qa` with two well-defined exit
conditions. Lifting it out would be a hypothetical seam (one adapter,
no second).

### 3. Architecture lens permanently in BASE_AGENT_INSTRUCTIONS

The system prompt now includes a `## Architecture (mandatory lens)` block
listing the LANGUAGE.md vocabulary, the deletion test, and the
"one adapter is hypothetical, two is real" rule. `_CRITICAL_RULES` in
the coding prompt has a matching `### Architecture (deepening lens)`
section requiring the agent to name its deepening choice in the commit
message for any change that touches a module's interface.

Intent-routed skill auto-loading: when the LLM-extracted intent has
`change_type == "bugfix"`, the coding prompt prepends
"call `skill(name='diagnose')`"; for `"feature" | "refactor" |
"performance"` it prepends `tdd` + `improve-codebase-architecture`.
Other types (docs/config/test) get no directive.

### 4. Architecture Mode — continuous deepening loop

Four new columns on `freeform_configs`: `architecture_mode` (bool),
`architecture_cron` (str), `last_architecture_at` (timestamp),
`architecture_knowledge` (text). When `architecture_mode = True`, the
new `agent/architect_analyzer.py` (parallel to `agent/po_analyzer.py`)
runs the agent on a readonly clone, mandates loading
`improve-codebase-architecture`, and produces 3–5 `Suggestion` rows with
`category="architecture"`. The existing auto-approval path turns these
into Tasks; we set their `intake_qa = []` so they skip the grill phase
(the analyzer has already grilled itself).

Migration `021_intake_qa_and_architecture_mode.py` ships both schema
changes (intake_qa + the four FreeformConfig columns) in one upgrade.

### Skill precedence on name collision

`skills/engineering/` is consulted before `superpowers/skills/`. Rationale:
the engineering skills are this repo's locally curated content with the
`docs/decisions/` rewrite already applied. If a future
superpowers update adds a same-named skill, our local version wins;
operators who want the upstream version delete the local one.

## Consequences

- **Positive**: Plans now align with the user up front rather than at
  review. The deepening lens is enforced both in the system prompt
  (every turn) and in the coding critical rules (every coding task),
  not only when the user remembers to ask. Architecture Mode gives a
  programmable surface for "make this codebase more navigable over
  time" without manual ticket-filing. The `tdd` and `diagnose` skills
  auto-load by intent, so the LLM doesn't have to remember.

- **Negative**: A new schema column (`tasks.intake_qa`) and four
  freeform-config columns are migration debt. Architecture Mode adds a
  new background coroutine to `run.py`'s task list, increasing the
  number of long-running periodic loops in the process. The grill phase
  adds latency to complex tasks (3–7 user round-trips before any plan).

- **Trade-off rejected**: We considered making `improve-codebase-architecture`
  a skill the LLM *could* load on its own rather than baking the
  vocabulary into BASE_AGENT_INSTRUCTIONS. The LLM forgets. Baking it in
  costs ~80 tokens per turn but guarantees the lens is applied.

- **Out of scope (deferred)**: A web-next UI toggle for Architecture Mode.
  The freeform sidebar already exposes PO config; an architecture
  toggle would slot beside it but is a follow-up task — backend ships
  first. The TS types are already regenerated so the frontend can be
  added without further schema work.
