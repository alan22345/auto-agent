# [ADR-015] Task flow redesign — three classifications, freeform mode, no-defer enforcement

## Status

Proposed

Supersedes ADR-013 (trio drives backlog via subagents). Extends ADR-014 (split decision contract — prose by heavy model, schema by Haiku) by generalising its skills-bridge pattern from "trio submit-X tools" to all gated agent actions across all three flows.

## Context

Three concrete failures on the production VM motivate this redesign. They look like separate bugs but share a root cause: the system has *one* fully-wired flow (the trio for complex_large tasks) and treats the simple / complex flows + freeform mode as ad-hoc decorations. The trio inherits its smoke gap from the regular flow's smoke gap; the regular flow inherits its no-defer gap from the absence of a no-defer rule anywhere; the trio's submit_X tools are invisible to CC pass-through and the orchestrator has no fallback.

**Failure 1 — Deferred work shipped past every gate (task 170, PR #43, 2026-05-14).**

The architect produced a backlog where `CounterfactualWorld.fork_from` was a stub labelled "Phase 1 fills this in later." `Phase 1` was never in the backlog. The endpoint `/api/counterfactual/start` was reachable, the route's handler called `fork_from`, and the first user click returned a 500 with `raise NotImplementedError`. Five reinforcing gaps:

1. **Per-item reviewer is alignment-only.** The prompt is "the spec is authoritative — don't reject for things the work item didn't ask for." The scaffold commit that introduced the stub landed *before* T1, so it was outside every per-item diff the reviewer ever saw.
2. **`architect.checkpoint` never converged** — the topic of ADR-014 — so the final integration gate didn't run; Alan opened the PR manually, bypassing every downstream check.
3. **`agent/lifecycle/verify.py::handle_verify`** (the boot+intent check that *would* have caught this on a regular complex task) is **skipped entirely by trio.** Dispatcher goes from per-item reviewer to PR.
4. **Reviewer is readonly with "no tests, no shell"** in its prompt; even if it had been shown the scaffold diff, it couldn't have grepped, booted, or hit the route.
5. **No layer of the system** — not the architect prompt, not the backlog validator (there isn't one), not the diff inspection (also doesn't exist), not the PR gate — **rejects "phase 1" / "TODO(phase)" / `raise NotImplementedError` reachable from added code.** Verify.py for the regular flow has the same hole.

**Failure 2 — Smoke + UI verify is fragmented.**

`verify.py::handle_verify` boots the dev server and does an intent check on regular complex tasks. Trio doesn't call it. The PR review gate doesn't grep the diff for stubs. UI changes are not visually inspected on either flow. The same logical primitive (boot → exercise affected routes → inspect UI → grep diff for stubs) is needed in at least three places and currently exists in zero.

**Failure 3 — Custom Python tools are invisible in CC pass-through.**

`LLM_PROVIDER=claude_cli` (the prod default) routes through `AgentLoop._run_passthrough`, which calls `claude --print`. CC uses its own Read/Edit/Write/Bash/Grep/Glob; the Python-side `AgentLoop.tools` registry is bypassed. ADR-014 documented this for `submit_*` and side-stepped it for trio decisions with a Haiku extractor. The same problem applies to every other structured agent output we'd want — the architect's design doc, the per-item review verdict, the final review's gap list, the PR review's comments. Each is a candidate for either (a) regex extraction (brittle, ADR-014 already lit this fire twice) or (b) a fresh structured-output pipe (one per call site, lots of duplication). Neither is sustainable as the flow shape grows.

**The deeper context — the three flows the system was supposed to provide.**

Auto-agent classifies tasks into `simple` / `complex` / `complex_large` (`agent/classifier.py`). The user's UX has these flows:

- **simple**: grill-me → one-shot → raise PR → self-review against the grilled intent → tell user.
- **complex**: grill-me → write plan → user approves plan → execute → verify code + UI + smoke → raise PR → self-review → address own comments → tell user.
- **complex_large**: grill-me → architect designs → dispatch subagents that write code → per-subtask review (alignment + smoke + UI) until all subtasks done → final review against the architect's plan → architect closes gaps from its persisted context → raise PR → self-review → address own comments → tell user.

In all three the human is in the loop at two points only: a planning/design approval, and the final PR review. A second operational mode — **freeform** — replaces the human at every gate with an agent that has more context: PO (for PO-suggested tasks) or improvement agent (for codebase-deepening tasks, currently named "architecture mode" via `agent/architect_analyzer.py`).

The current code has fragments of all of this — a classifier, a trio for complex_large, a `po_agent.py` that answers clarifications in freeform, a weekly cron loop for architecture suggestions — but they don't compose into the user's intended three-flow shape. The trio is the only path that meaningfully exists end-to-end, and it's the path that broke.

## Decision

**Adopt a single coherent three-flow architecture with freeform as an orthogonal mode, shared verify primitives, four-layer no-defer enforcement, and a generalised skills-bridge for all gated agent actions.**

The decision has twelve load-bearing components. Each is locked by name; numbering matches the grill-me branches the design walked through.

### 1. Three flows, one classifier, conditional grill across all of them

Classifier (`agent/classifier.py`) keeps its three labels (`simple` / `complex` / `complex_large`) and adds one field: `needs_grill: bool`, returned in the same `complete_json` call as `classification`. The flow then runs grill-me only if `needs_grill` is true — the existing `_SKIP_GRILL_COMPLEXITIES` set goes away. Trivial / unambiguous tasks ("rename this variable to snake_case") skip grill regardless of classification.

`ClassificationResult` (in `shared/types.py`) gains `needs_grill: bool`. Existing callers default it to `True` for backwards-compat-while-migrating; the classifier always returns an explicit value.

### 2. complex_large approval artefact is one design doc

Between the architect's design pass and any builder dispatch, the orchestrator writes `.auto-agent/design.md` (the architect's output) and waits for `.auto-agent/plan_approval.json` (the user's verdict in non-freeform, the standin's in freeform). Approval is binary at the doc level: `{verdict: "approved" | "rejected", comments: str}`. Mid-flight backlog edits are allowed **within** the approved design (architect re-slices); structural changes that contradict the design escalate back for re-approval.

No "approve item list separately" gate. The design doc is the single approval artefact for the whole complex_large run, including any future sub-architect spawns.

### 3. Per-item reviewer in complex_large is one heavy agent doing alignment + smoke + UI

The "readonly alignment-only reviewer" from ADR-013 is replaced with a heavy reviewer that:

- reads the item spec and the item diff,
- runs `verify_primitives.exercise_routes(item.affected_routes)` (the routes the architect declared for the item, plus any routes the builder appended),
- runs `verify_primitives.inspect_ui(route, intent)` for each affected route that touches UI files,
- runs `verify_primitives.grep_diff_for_stubs(diff)`,
- writes `.auto-agent/reviews/<item_id>.json` via the `submit-item-review` skill.

The previous bifurcation (alignment readonly + smoke separate) is rejected because **subtask sizing already guarantees each item is substantial enough to warrant a full review pass.** If a unit of work isn't big enough to justify smoke+UI, it isn't a subtask — fold it. (See §9.)

### 4. complex_large final review is a new agent reading the design doc as context

After the per-item loop drains, the orchestrator dispatches a **final reviewer agent** (new role) with:

- the design doc (`.auto-agent/design.md`) as its primary context,
- the full integrated diff,
- all per-item review summaries,
- the original grill output.

Final reviewer runs smoke + UI across the *union* of all affected_routes from every item, and writes `.auto-agent/final_review.json` via the `submit-final-review` skill:

```json
{"verdict": "passed", "comments": "..."}
{"verdict": "gaps_found", "gaps": [{"description": "...", "affected_routes": [...]}, ...]}
```

On `gaps_found`, the **architect's persisted session resumes** (see §13) and produces a fresh backlog of items to close the gaps. The new items go through the normal builder → heavy-review loop. Final reviewer is re-invoked **fresh each round** (no persisted reviewer session — the design doc + previous-gap-list + previous-attempt-summary are attached to the prompt explicitly).

Gap-fix loop is bounded at **3 rounds**. After 3, escalate to user (non-freeform) or improvement agent (freeform).

### 5. Self-PR-review for every flow

After a PR is opened, a PR-reviewer agent reads the PR as a teammate would: PR title, description, commit narrative, diff in GitHub's rendering, CI signals. Writes `.auto-agent/pr_review.json` via the `submit-pr-review` skill. Then **the same agent addresses its own comments** in one round (push fix-up commits or rewrite the PR description). Then signals the user (or auto-merges in freeform if the standin approves).

Two prompt shapes for this agent depending on flow:

- **complex / complex_large** — PR-as-artefact lens (hygiene, commit narrative, missing tests, PR description coherence). Doesn't re-run smoke (final-review did).
- **simple** — PR-as-correctness lens. This is the *only* full verify gate the simple flow has (no plan-approval, no final review), so the PR review runs `verify_primitives.*` end-to-end against the PR diff.

If the PR review surfaces real correctness gaps on complex / complex_large, that's a final-review escape — bounce back to the architect via `gaps_found`, do **not** try to fix in the PR-review gate.

### 6. Freeform-mode standins are origin-based

In freeform, the standin at every gate is determined by the task's origin:

- task came from a PO suggestion → **PO agent** stands in (extending `agent/po_agent.py::answer_architect_question` to also answer grill questions and gate plan/PR approvals; context source: `Repo.product_brief` + `ARCHITECTURE.md`).
- task came from an improvement-agent suggestion → **improvement agent** stands in, with its persisted session/state from the codebase-deepening loop as context.
- user-created task → PO by default.

Standins log every decision with agent ID + cited context refs, surfaced in web-next under the task's gate history. When the standin lacks relevant context for a question, it picks a reasonable default and logs `fallback_default(source=heuristic)` — it does **not** escape to the user; that defeats freeform.

### 7. Mode flag — per-repo default + bidirectional per-task UI toggle

`Repo` model gains `mode: "freeform" | "human_in_loop"` (default: `human_in_loop`). Each task can override the repo default in *either* direction via a UI toggle at intake (`Task.mode_override: Literal["freeform","human_in_loop"] | None`). The orchestrator resolves effective mode as `task.mode_override or repo.mode`. There is no asymmetry — a freeform repo can force-human-review a specific task and vice versa.

### 8. Four-layer no-defer enforcement

Stubs and deferred work are rejected at four independent points:

1. **Prompt rule** in `ARCHITECT_INITIAL_SYSTEM`, `ARCHITECT_CHECKPOINT_SYSTEM`, builder system prompt, reviewer prompt, final-reviewer prompt:
   > "Never produce deferred work. Never emit `raise NotImplementedError`, `# TODO(phase`, `pass # placeholder`, 'Phase 1 fills this in', 'v2 ships', 'in a future PR', 'later' in code or backlog items. If the work is too big for one item, split into more items. If genuinely huge, spawn sub-architects."
2. **Backlog text validator** (`agent/lifecycle/trio/validators.py`, new) — regex over backlog item titles + descriptions for forbidden phrases. Rejects at architect submission time with a structured rejection that flows back into the architect's next turn.
3. **Diff-hunk grep at every smoke step** — `verify_primitives.grep_diff_for_stubs(diff)`, called identically by trio per-item review, complex-flow `verify.py`, final review, and PR review. Patterns: `raise NotImplementedError`, `# TODO(phase`, `pass  # placeholder`, `# Phase \d`, `# v2 will`, `# in a future PR`, plus the backlog-text list. Scope: **added lines in diff hunks only** (not full file). False-positive filter: excludes `tests/`, `test_*.py`, `*.md`, `*.mdx`. Inline opt-out: `# auto-agent: allow-stub` on the line; every opt-out appears in the PR description for human visibility.
4. **PR-review gate as backstop** — re-runs the same grep against the full PR diff before signing off.

Four layers may sound paranoid; each catches a *different* failure mode (LLM compliance, slipped text, slipped code, slipped past everything). Removing any one is a known failure path (failure 1 above happened because layers 2–4 didn't exist).

### 9. Subtask sizing — scrum-points framing, structural validator, no item cap

Architect prompt (replacing the file-count heuristic):
> "Each backlog item must be at least a 5-pointer in a scrum team — something that would be a standalone PR on its own. If you would describe an item in one sentence, it is too small; merge. If you cannot fit an item in one design pass, consider spawning sub-architects."

Structural validator on every backlog item: title, description ≥80 words, `justification` ("why is this its own slice"), `affected_routes: [str]`, `affected_files_estimate: int`. Items failing the structural shape are rejected with feedback in the architect's next turn.

**No item cap.** Auto-agent must handle complex_large runs with many items. The forcing function on slicing is the scrum-points floor + the validator, not an item count ceiling.

**Sub-architect trigger.** Architect can emit `decision = "spawn_sub_architects"` in `.auto-agent/decision.json` instead of a backlog, listing named slice boundaries:

```json
{"action": "spawn_sub_architects", "slices": [{"name": "auth", "scope": "..."}, ...]}
```

Each slice runs as a child architect with its own `.auto-agent/slices/<name>/design.md`, its own grill (if `needs_grill`), its own builder→review loop. **Sub-architects run serially** (so the parent's session is alive to answer grill questions; see §10). Recursion bounded to **1 level** — a sub-architect that itself wants sub-sub-architects escalates: the task was mis-classified and should have been split at intake.

### 10. Sub-architect grill questions are answered by the parent architect, not the user

In every mode (freeform or human-in-loop), sub-architect grill questions go up to the parent architect, never to the user or to a PO/improvement-agent standin. The parent has the full design context and is the right oracle. Relay shape via the skills bridge:

- Sub-architect emits `awaiting_parent_grill_answer` via `submit-grill-question` skill → writes `.auto-agent/slices/<name>/grill_question.json`.
- Sub-architect process exits; orchestrator picks up the file.
- Orchestrator resumes the parent architect's session, prepending the question to its next turn.
- Parent answers via `submit-grill-answer` skill → writes `.auto-agent/slices/<name>/grill_answer.json`.
- Orchestrator re-invokes the sub-architect, prepending the answer.

### 11. Shared verify primitives in `agent/lifecycle/verify_primitives.py`

Four pure functions, called identically by trio per-item review, complex-flow `verify.py`, final review, and PR review:

- `boot_dev_server() -> ServerHandle` — reads `auto-agent.smoke.yml` (declares `boot_command`, `health_check_url`, `boot_timeout`). Auto-detect fallback: `package.json` `dev` script → `run.py` → `make dev`. Polls health URL until 200 or timeout. Returns handle for teardown.
- `exercise_routes(routes: list[Route]) -> dict[Route, RouteResult]` — for each route, GET (or POST with body from `auto-agent.smoke.yml` if declared); expect 2xx; capture response body. **Runtime stub detection**: response of `null` / `{}` / empty list / 500 with `NotImplementedError` in traceback = fail. `auto-agent.smoke.yml` can declare an `expected_shape` per route to override the default heuristic.
- `inspect_ui(route, intent) -> UIResult` — headless browser to route → screenshot → one LLM call (`{screenshot, intent}` → `PASS|FAIL` + one-sentence reason). Failures block.
- `grep_diff_for_stubs(diff) -> StubResult` — as specified in §8.

Per-repo config `auto-agent.smoke.yml` at repo root, created by the onboarding harness alongside CLAUDE.md/lint setup. Declares `boot_command`, `health_check_url`, `boot_timeout`, optional `post_bodies` keyed by route, optional `expected_shape` per route, optional `browser_path`.

`affected_routes` for an item is **architect-declared in the backlog item spec**, **builder can append** discovered routes during implementation, **builder cannot remove** architect-declared routes. Reviewer exercises the union.

### 12. Skills-bridge contract — `.auto-agent/` workspace

Generalising ADR-014's pattern: every gated agent action is a **skill** that tells CC to write JSON (or markdown) to a known path; the orchestrator reads after `agent.run` returns. Replaces ADR-014's Haiku extractor for *new* call sites; ADR-014's extractor remains in place for the trio decisions it covers, gradually deprecated as the new skills land.

Workspace layout under `.auto-agent/` at the repo root (replaces `.trio/` — one-shot rename, no compatibility shim):

```
.auto-agent/
├─ grill.json              # grill exit summary (all flows)
├─ plan.md                 # complex-flow plan
├─ plan_approval.json      # user/standin approval verdict
├─ design.md               # complex_large architect design
├─ backlog.json            # current backlog
├─ decision.json           # architect per-cycle decision
├─ reviews/<item_id>.json  # per-item heavy-reviewer verdict
├─ final_review.json       # complex_large final reviewer
├─ pr_review.json          # self-PR-review
├─ smoke_result.json       # last verify primitive run
├─ architect_log.md        # architect's externalized journal (see §13)
├─ decisions/<n>.json      # per-decision rationale snapshots
└─ slices/<name>/          # sub-architect namespace
   ├─ design.md
   ├─ backlog.json
   ├─ grill_question.json  # child → parent relay
   └─ grill_answer.json    # parent → child relay
```

Skills (one per write action, each writes one file and exits):

- `submit-grill-exit` → `grill.json`
- `submit-plan` → `plan.md`
- `submit-design` → `design.md`
- `submit-backlog` → `backlog.json`
- `submit-architect-decision` → `decision.json`
- `submit-item-review` → `reviews/<id>.json`
- `submit-final-review` → `final_review.json`
- `submit-pr-review` → `pr_review.json`
- `submit-grill-question` (sub-architect only) → `slices/<name>/grill_question.json`
- `submit-grill-answer` (parent architect only) → `slices/<name>/grill_answer.json`

Each output file carries `schema_version: "1"` for future evolution. Orchestrator behaviour when the expected file is missing after `agent.run` returns: retry with prompt amendment ("you must call the submit-X skill before stopping") up to **2 retries**, then escalate (user in non-freeform, standin in freeform). Without this, the orchestrator hangs silently — the failure mode that already bit ADR-014.

### 13. Architect persisted session with aggressive compact + externalized journal

The architect's session persists across an entire complex_large run (initial design → per-item review summaries arriving → checkpoint → gap-fix rounds). To stop the session from bloating, the architect:

- **compacts aggressively** — same `agent/context/autocompact.py` machinery the runtime already has, but with `.auto-agent/design.md`, `.auto-agent/backlog.json`, and the current `decision.json` always pinned (never compacted away).
- **externalizes its decisions to disk** — every time the architect emits a `submit-architect-decision`, an entry is appended to `.auto-agent/architect_log.md` with timestamp, decision, reason, and references to per-decision detail in `.auto-agent/decisions/<n>.json`.
- **re-reads the journal on demand** — when the architect needs detail about an earlier decision (e.g., "why did I slice item 3 the way I did"), it reads `architect_log.md` or the specific decision JSON via standard file_read. The journal is the source of truth; the session is a working buffer.

The same pattern applies to durable cross-task lessons: when the architect or improvement agent learns something worth surviving the task, it writes to team-memory (`mcp__team-memory__remember`).

### 14. Rename `architect_analyzer.py` → improvement agent

`agent/architect_analyzer.py` becomes `agent/improvement_agent.py`. Mode renames: "architecture mode" → "improvement mode". All UI labels, prompt references, ADR cross-refs, and memory entries update. The trio's *task-decomposer* architect (`agent/lifecycle/trio/architect.py`) keeps the name "architect" — it is a role within a flow, not a mode.

### 15. State wipe at deploy

Drop all `Task` rows and all `Suggestion` rows (PO + improvement). Per-task children — `ArchitectAttempt`, `TrioReviewAttempt`, `VerifyAttempt`, `intake_qa`, etc. — cascade with the parent Task. **Do not** wipe `Repo`, `User`, `Session`, or any non-task-scoped data. Auto-agent is an experiment; no migration logic for in-flight work under the old code paths, but per-repo state (`product_brief`, `ARCHITECTURE.md`, smoke configs, mode flag) stays so the system is immediately usable post-deploy.

### 16. TDD implementation

Every new component (verify primitives, skills, classifier extension, reviewers, mode-flag resolution, no-defer validators) lands with failing tests first. The Groundhog Day bug slipped through twice because it was never caught by tests; the same risk applies to every layer in this redesign.

## Consequences

**Easier:**

- One flow shape per classification, written down. Adding a new classification or a new gate becomes "extend the skill set + extend the orchestrator state machine" — no more ad-hoc per-flow drift.
- One verify module called from four places. Regressing the no-defer rule on the regular flow without regressing it on trio (the failure mode 2 captured) is now impossible — they call the same function.
- Skills-bridge generalises the ADR-014 pattern: every structured agent output is a file write the orchestrator picks up. No more "this call site has a custom Haiku extractor, that call site has a regex, the third call site uses tool calls invisible in CC pass-through." One pattern, ten call sites.
- Freeform mode is no longer a special-case bolt-on; it's a flag that swaps the resolver at every gate. The plumbing is the same.
- Architect's persisted session stops being a token-bloat liability: the journal externalises the decision history so the session can compact freely. This is the same pattern as the operator's own memory system.
- TDD catches the regression class that hurt the most. Layered no-defer enforcement isn't paranoia; it's the deletion test.

**Harder:**

- This is a large change. Five new agent roles (heavy reviewer, final reviewer, PR reviewer, mode-resolver, parent-architect grill-relay handler) plus extending PO and improvement agents to cover more gates. The TDD discipline is what keeps this from turning into a regression mill.
- Per-task UI toggle for freeform mode means the web-next UI has new state. The mode resolver becomes a UI-facing primitive, not just an orchestrator one.
- `auto-agent.smoke.yml` is a new per-repo onboarding artefact. Repos already in the system need one written; the onboarding harness extension handles new repos.
- The architect's persisted session is a moving target — it has to survive heartbeat windows, compact rounds, and gap-fix loops without losing the design doc. This is solved by pinning the design doc + the current backlog + the current decision in the autocompact policy, but it's a new invariant the runtime has to honour.
- Sub-architects run serially (parent's session must be alive to answer grills). For genuinely huge tasks this is a wall-clock cost. Mitigated by sub-architects only firing for tasks where serialisation is unavoidable anyway.
- Test surface grows substantially: 12 branches × multiple flows × freeform/non-freeform × every gate. `eval/` promptfoo fixtures need at minimum: stub-introduction-blocked (the task 170 reproduction), design-approval-required-before-dispatch, sub-architect-spawn-parent-answers-grill, freeform-standin-decision-logged.

**Deletion test:** if a future contributor removed the four no-defer layers and a future architect emitted a stub backed by a "Phase 1 fills this in later" comment, the original failure 1 above returns verbatim. Same for skipping the shared verify primitive from any one flow — the regression that motivated this ADR is reintroduced.

## Migration plan

- Branch off the current `auto-agent/trio-subagents-013`.
- Drop all `Task` / `Suggestion` / `ArchitectAttempt` / `TrioReviewAttempt` rows in a single migration. No data preservation; this is an experiment.
- Phase the implementation under TDD discipline (failing test first per component). Suggested order:
  1. `verify_primitives.py` + tests (`exercise_routes`, `inspect_ui`, `grep_diff_for_stubs`, `boot_dev_server`).
  2. Classifier extension: `needs_grill: bool` returned alongside `classification`.
  3. Skills-bridge: skill files in `skills/auto-agent/` + workspace rename `.trio/` → `.auto-agent/`.
  4. Simple flow restructure: conditional grill + one-shot + PR + self-review (correctness scope).
  5. Complex flow: plan-approval gate + wire verify primitives + self-PR-review (artefact scope).
  6. Complex_large architect reshape: design doc gate + structural backlog validator + scrum-points prompt + persisted session + externalized journal + autocompact-pin policy.
  7. Complex_large reviewers: heavy per-item reviewer (alignment + smoke + UI in one agent) + new final-reviewer role + PR-reviewer.
  8. Sub-architect spawn + parent-answers-grill relay.
  9. No-defer 4-layer enforcement (prompt + backlog validator + diff-grep + PR backstop).
  10. Mode flag: per-repo + bidirectional per-task UI toggle + standin routing in `po_agent.py` + `improvement_agent.py`.
  11. `architect_analyzer.py` → `improvement_agent.py` rename across code + UI labels + memory references.
  12. Web-next UI: design doc approval surface, freeform toggle on task intake, gate-history audit panel.
  13. `eval/` fixtures for the four new failure modes.

Each phase ships green tests + lint + a deploy to the VM + a watched run before the next phase starts.

## Related

- ADR-013 — superseded; the trio's per-item reviewer reshape and the skills-bridge pattern subsume its decision space.
- ADR-014 — extended; its split-prose-from-schema insight is generalised into a skill-per-gate contract, with the orchestrator owning file reads.
- ADR-009 — lifecycle phase modules; this ADR adds new phases (plan-approval, design-approval, PR-review) under the same module pattern.
- ADR-010 — structured LLM output; `complete_json` remains the primitive for any structured output that doesn't go through the skills bridge (e.g., the classifier).
- ADR-003 — engineering skills vendoring; the same loading mechanism is reused for the `submit-*` skills.
- Memory: `claude_cli_no_custom_tools.md` (the constraint that motivated the skills bridge), `architecture_decisions.md` (concurrency cap), `freeform_mode.md` (the mode this ADR formalises).
- Task 170 incident (2026-05-14) — the deferred-stub failure that prompted this ADR.
