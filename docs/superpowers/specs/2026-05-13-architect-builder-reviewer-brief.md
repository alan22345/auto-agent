# Sub-Project B — Architect / Builder / Reviewer Trio — Pre-Brainstorm Brief

**Status:** Handover. Ready for the brainstorming skill to turn this into a design spec.
**Date:** 2026-05-13
**Predecessor specs:**
- Sub-project A shipped: `docs/superpowers/specs/2026-05-12-bigger-po-market-research-design.md`
- Sub-project C shipped: `docs/superpowers/specs/2026-05-12-freeform-self-verification-design.md` + plan + 34 commits on `main` (range `d28ee46..e5f87a1`).

## What the user asked for

A way for the agent to bootstrap **cold-start, demo-grade repos** — i.e. take a fresh idea ("build me a recipe app with voice search") and produce a working, runnable, visually-coherent first cut. Not just one PR on an existing codebase; the whole zero-to-demo arc.

The original brainstorming session (the one that produced A and C) split this into three roles that need to negotiate:

- **Architect** — owns shape: tech stack, file layout, key data model, the few load-bearing decisions a fresh repo needs before code starts.
- **Builder** — implements against the architect's brief. Currently the closest analogue to today's coding-phase agent.
- **Reviewer** — judges *demo-worthiness*: does this look like a real product, does it run, does it match what the user actually asked for?

The unique thing about B (vs. the existing planner→coder→reviewer flow) is that the three agents iterate against *each other*. Architect can revise after reviewer pushback. Builder can request architectural clarification mid-build. The protocol is the design question.

## Why B is next

A solved "PO suggestions need grounding outside the codebase."
C solved "the agent ships code without observing the running system."
B is the only one of the three that fundamentally changes *what kind of task auto-agent can take on* — moving from "improve an existing repo" to "build something from nothing." A and C were prerequisites: B's reviewer needs C's `browse_url` + dev-server lifecycle to judge demo-worthiness, and B's architect needs A-style outside grounding to pick a stack that fits the market.

## What B can build on (now that C has shipped)

Concrete capabilities the trio inherits:

| Capability | Where | Notes |
|---|---|---|
| Dev-server lifecycle | `agent/tools/dev_server.py` | `sniff_run_command`, `start_dev_server` (async CM), `wait_for_port`, `hold`, `kill_server`. Used by coding / verify / review today. |
| Visual capture | `agent/tools/browse_url.py` | Single screenshot tool; pack `{http_status, text, screenshot_base64}` in `ToolResult.output`. Reviewer agent calls this directly. |
| `with_browser=True` agents | `agent/lifecycle/factory.py::create_agent` | Flag exposes `browse_url` + `tail_dev_server_log` to any agent loop. |
| Attempt audit tables | `verify_attempts`, `review_attempts` | Schema pattern for "agent attempted X, succeeded/failed, with reasoning + tool_calls". |
| Web search + fetch | `agent/tools/web_search.py`, `agent/tools/fetch_url.py` | From A. Lets the architect look outside before deciding. |
| Market brief grounding | `MarketBrief` table | The architect for B can reuse this to ground "what should this product look like." |
| Two-cycle retry pattern | `verify.py` + `review.py` | Generalisable shape: persist attempt → ok → ship; not-ok → loop back; second not-ok → BLOCKED. |
| Affected-routes contract | `Task.affected_routes` jsonb | Planner declares routes; verify + review consume. The architect agent has an analogous "what's the surface area of this repo" output it could persist similarly. |
| Tool-call audit | `*_attempts.tool_calls` jsonb | Pattern for "the agent did things; here are the URLs it browsed and the screenshots it took." Useful for the reviewer's demo-worthiness verdict. |

What B will need that doesn't exist yet (likely):

- A workspace-bootstrap step (scaffold a fresh repo from a stack template, not clone an existing one).
- An "architect output" persistence shape (the analogue of `Task.plan`, but structured).
- A protocol layer where the three agents exchange messages without leaking each other's context. Maybe a new `TaskRole` enum on `TaskHistory` so the audit trail shows which agent said what.
- A demo-worthiness rubric — concrete enough for the reviewer to apply consistently across very different stacks.

## The big open questions for the brainstorming skill

These are real choices with no obvious right answer:

1. **Workflow shape: serial trio, or negotiation graph?**
   - Serial (architect → builder → reviewer → done) is simple but loses the "agents argue" property.
   - Graph (architect proposes, builder/reviewer can each push back, architect revises, repeat) is closer to a real team but explodes the state machine.

2. **Where does the trio plug into the existing lifecycle?**
   - As a new "scaffold" mode that produces a repo, after which today's freeform mode takes over.
   - As a replacement for planning+coding+review when `task.source` is "new project".
   - As its own parallel flow with its own state-machine slice.

3. **What is "demo-grade"?**
   - Boots, has a home page that renders without errors, addresses the user's ask in *some* visible way.
   - Or stricter: covers N user flows, passes basic a11y checks, has a written README that matches what's there.
   - Or even stricter: a small set of hand-curated quality bars (typography readable, no debug text, no Lorem Ipsum).
   - This question shapes the reviewer's prompt and how aggressively the trio loops.

4. **Stack selection: hard-coded options or open-ended?**
   - Curated list (Next.js + FastAPI + Postgres, or Astro + SQLite, etc.) the architect picks from. Bounded, repeatable, but limiting.
   - Open-ended where the architect proposes any stack given the task. More flexible, but quality varies wildly and the verify/review infra (`sniff_run_command`) has to work for whatever it picks.

5. **Repo creation: GitHub, local-only, or workspace-only?**
   - The agent already creates branches and PRs on existing repos via `gh`. New-repo creation through `gh repo create` is straightforward but commits to a real GitHub presence per attempt.
   - Workspace-only (the agent works in `.workspaces/<task-id>/` and never pushes anywhere until a human "promote" step) is safer for experimentation.
   - The user's freeform-mode pattern is auto-merge on green CI; cold-start equivalent isn't defined.

6. **State sharing between the three agents:**
   - Shared workspace + git history (each agent commits their work; the next reads `git log`). Simple, matches today.
   - Explicit handoff documents (architect produces `ARCHITECTURE.md`, builder reads it, reviewer reads both diff + ARCHITECTURE.md). More structured.
   - Both.

7. **Failure handling:**
   - C uses a 2-cycle budget then BLOCKED. Should B do the same per-role? Per-iteration?
   - What does "BLOCKED" mean for a half-built cold-start repo? Delete the workspace? Surface the partial output for human inspection?

8. **Cost / latency:**
   - Three agents iterating is at least 3× the LLM cost of a single coding pass. Some tasks will take 30+ minutes wall time. Worth budgeting (turn cap per role; time cap per session; max iterations).
   - The user has been comfortable with C's 3+ server boots per task. B is heavier.

## Suggested approach (a starting hypothesis, not a decision)

A new `bootstrap` task mode, parallel to today's planning→coding flow:

- **State machine slice**: `INTAKE → CLASSIFYING → SCAFFOLDING → ARCHITECTING → BUILDING → BOOTSTRAP_REVIEW → VERIFYING → AWAITING_REVIEW → DONE`. The verify and review phases from C are reused as-is — they already handle "run it and look at it."
- **`agent/lifecycle/architect.py`** — readonly agent run. Inputs: task description, optional market brief. Output: `architecture.json` (stack choice, file layout, route list, data model sketch) + an `ARCHITECTURE.md` written to the workspace. Persisted as a new `architect_attempts` table.
- **`agent/lifecycle/scaffolding.py`** — non-LLM. Reads `architecture.json`, runs the corresponding template generator (`npx create-next-app`, `uv init`, etc.). One row per template. This is the only stage where we commit to a stack.
- **`agent/lifecycle/builder.py`** — essentially today's coding phase, but with the architect's contract loaded into the system prompt and `ARCHITECTURE.md` re-read between turns.
- **`agent/lifecycle/bootstrap_review.py`** — the new reviewer role. Different from today's `review.py` because it doesn't review a PR diff — it reviews a *whole repo's first impression*. Uses `browse_url` heavily. Emits a structured verdict against a demo-worthiness rubric. Loops back to architect or builder depending on the issue type.
- **Curated stacks for v1**: Next.js + Tailwind (frontend), FastAPI + SQLite (backend), Next.js + FastAPI + Postgres (full-stack). Three templates. Open-ended stack picking deferred.

This is an opening offer. The brainstorming session should test it against the user's actual constraints — they may want something simpler (just an "architect that writes a plan" without changing the state machine) or richer (true multi-agent negotiation with messaging).

## How to start

1. Read this brief.
2. Read `docs/superpowers/specs/2026-05-12-freeform-self-verification-design.md` to understand the format and rigor expected. The user reviewed and approved it; match its bar.
3. Skim `docs/superpowers/plans/2026-05-12-freeform-self-verification.md` to see how a spec turns into 32 bite-sized TDD tasks.
4. Invoke `superpowers:brainstorming`. The brief above gives you the scope-flag step for free; jump to clarifying questions on the 8 open questions.
5. Output a design spec at `docs/superpowers/specs/2026-05-13-architect-builder-reviewer-design.md`, then a plan, then execute via subagent-driven development.

## Context the next agent needs about the user

(Same as the C brief — copying for the next agent's convenience.)

- Personal project. Strongly prefers simplicity; will reject over-engineering.
- Treats the VM (`azureuser@172.190.26.82`) as their dev environment — direct deploys are fine.
- Works in `main` directly, often with multiple agents committing concurrently. Expect to coordinate.
- Reviews specs/plans carefully and pushes back on incoherence (caught a redundant Optional parameter in spec A; reshaped C's verify/review split three times before approving).
- Wants "work without stopping for clarifying questions" mode — make reasonable calls, ask in batches when you must, don't ping-pong.
- Comfortable with subagent-driven execution but **will interrupt and demand sync work** if a subagent gets stuck or makes uncoordinated edits (this happened in C between T19 and T20).
- Pushes to origin only on explicit request. Don't push without asking.

## Loose ends from sub-project C (worth fixing before or during B)

These are non-blocking but real. Most are flagged in the code-review transcripts from the 34 session commits.

### Bugs / robustness

1. **Dev-server log files leak in `/tmp`.** `start_dev_server` writes to a `NamedTemporaryFile` (`agent/tools/dev_server.py:135`) but `kill_server` never unlinks it. Long-running VMs accumulate `dev-server-*.log` files. Fix: `os.unlink(handle.log_path)` in `start_dev_server`'s `finally` after `kill_server` returns (or in `kill_server` itself).

2. **`BootError` not caught in `verify._run_verify_body`.** The `if run_cmd:` guard prevents it in normal use, but a race or fork failure would let it escape `handle_verify` uncaught. Add `except dev_server.BootError` next to the existing `BootTimeout` / `EarlyExit` handlers.

3. **`task.branch_name` could be None in `verify._pass_cycle`.** No defensive check; would produce `git push origin None` on an unusual task path. Add an explicit guard.

4. **`asyncio.wait_for(..., timeout=120)` envelope wraps `_open_pr_and_advance` in verify.** If `gh pr create` hangs inside the envelope, cancellation fires mid-network-call. Move the wait_for to wrap only the boot+intent stages, not the PR-creation handoff.

5. **TOCTOU race in `_allocate_port`.** `socket.bind(0)` allocates and releases; between then and the child's bind, another process could grab the port. Acceptable as v1; would surface as `wait_for_port` succeeding but connecting to the wrong server. Mitigate at detection time if it ever matters.

6. **OK-regex tightened during C but adjacency cases worth eval.** `_INTENT_OK_RE` now requires `^OK\s*$` on the first line. Real LLM output may include trailing whitespace or capitalization variants ("Ok") — keep an eye on it in the eval.

### Tests

7. **`tests/test_verify_review_models.py` fails against a Postgres DB that isn't at HEAD 032.** It's the only test in this feature that requires a live DB; the others use the conftest's skip pattern correctly. The previous reviewer reported it passes against local Postgres at `:5432` with migration 032 applied. The auto-agent Docker container was crash-looping on a separate pre-existing schema lag during the session — that's the environmental fix, not a code fix.

8. **No agent-eval case yet for verify/review.** The spec called out "PO catches intent mismatch" and "review catches visually-broken UI" as eval follow-ups. The eval suite is in `eval/`; pattern is in the existing `eval/` cases.

### UI polish

9. **`AttemptsPanel` doesn't render screenshot thumbnails.** `tool_calls` contains the `browse_url` invocations, but the component only lists URLs in text. A future iteration could fetch the screenshot bytes (currently base64-encoded in the JSON payload of `tool_calls[i].result`) and display them inline.

10. **No screenshot persistence to disk.** Today, screenshots live in the agent's tool-call audit JSON only. The spec's "screenshots in `var/verify-screenshots/<task-id>/<phase>/<cycle>/`" was deferred (the `tool_calls` audit captures the same data inline). If screenshot thumbnails ever ship in web-next, consider extracting + serving them as static files.

### Code-organisation drift

11. **`shared/models.py` is now 624 lines.** Past the project's 500-line guideline (CLAUDE.md). Next ORM addition should trigger a split — `shared/models/freeform.py` for `VerifyAttempt` / `ReviewAttempt` / `FreeformConfig` / `Suggestion` would be the natural carve-out.

12. **`agent/tools/__init__.py::create_default_registry`** now has four flags (`with_web`, `readonly`, `with_browser`, plus the existing). If B adds more capability bundles, consider a builder pattern.

### Deferred (called out in spec)

- Golden-image comparison for visual regressions.
- E2E flow tests (click X then assert Y).
- User-declared structured assertions in `FreeformConfig` (a `verify_assertions: [{kind, path, expect}]` list).
- Cross-phase dev-server reuse (currently each phase boots its own; B will inherit this pattern and may want to optimise).

## What this session produced (for grounding)

- 34 commits on `main`, range `d28ee46..e5f87a1`, all on origin.
- One Alembic migration: `migrations/versions/032_verify_review_attempts.py` (idempotent enum adds, two new tables, two new columns).
- Spec C went through three substantial revisions during brainstorming (always-run-verify, split verify/review, then tool-driven visual capture) before approval.
- Plan C executed via subagent-driven development for T1–T19, then synchronously for T20–T32 after a parallel-edit conflict between subagents at T20. The takeaway: subagent execution for this size of plan works but isn't free of coordination cost — bundle tightly coupled tasks, and don't be surprised when the user asks to switch to sync mode.
- The full new-test fast path: 75 tests, ~2.2s. Slow path: 1 real-Playwright test, ~2s with cached Chromium.

The pattern Spec B should follow:

- Mirror C's spec/plan format — problem, out-of-scope, architecture, data model, components, tests, acceptance criteria.
- TDD throughout. Identify the regression test that's load-bearing — for B it's probably "trio does not declare a repo demo-ready when [obvious flaw], even if all three agents reported OK."
- Keep concerns isolated: lifecycle layer, tools layer, prompts layer, web-next layer — same module boundaries A and C respected.
- Persist every agent attempt as a row (the `*_attempts` pattern). The audit trail is what makes the trio debuggable.
