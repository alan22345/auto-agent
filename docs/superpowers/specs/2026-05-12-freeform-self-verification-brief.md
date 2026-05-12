# Spec C — Freeform Self-Verification — Pre-Brainstorm Brief

**Status:** Handover. Ready for the brainstorming skill to turn this into a design spec.
**Date:** 2026-05-12
**Predecessor specs:** Sub-project A shipped (`2026-05-12-bigger-po-market-research-design.md`). Sub-project B (architect/builder/reviewer trio) is queued behind this.

## What the user asked for

> "On freeform mode autoagent has no way of testing its own code via running or visual feedback — how can we incorporate this?"

The freeform-mode agent today writes code, commits it, opens a PR, and that's the loop. It can run `pytest` via `test_runner` and `bash` commands, but it has no way to:

- Start the project's dev server and see whether the app boots.
- Visit a route and see what renders.
- Capture a screenshot of a UI change it just made.
- Loop back: "the page is broken — let me fix it."

The core gap: **the agent ships code without ever observing the running system it just modified.** For UI work especially, "the tests pass" is not the same as "the feature actually works."

## Why we're doing C before B (sub-project order from the brainstorming session)

Sub-project B is "architect ↔ builder ↔ reviewer trio for cold-start demo-grade repos." B's reviewer agent will need to evaluate "is this demo-worthy" — a question that requires actually running the thing. If we build B before C, we ship a beautiful multi-agent negotiation protocol with no way to verify its output. C is the horizontal capability both freeform tasks (existing) and B (future) need.

## Existing landscape (what's there)

Tools the agent already has (`agent/tools/`):
- `bash.py` — arbitrary shell execution (600s cap, output truncation).
- `test_runner.py` — structured test runner, auto-detects framework.
- `file_read.py`, `file_write.py`, `file_edit.py`, `glob_tool.py`, `grep_tool.py`, `git.py`.
- `fetch_url.py` — HTTP GET + HTML → markdown. Built for the market researcher (Spec A) but available.
- No browser tool. No screenshot tool. No dev-server lifecycle tool.

Workspace lifecycle (`agent/workspace.py`):
- `clone_repo` creates an isolated workspace per task at `.workspaces/<task-id>/`.
- The agent runs inside that workspace.
- No port allocation / no dev-server slot management today.

The agent loop (`agent/loop.py`):
- Multi-turn tool-calling. Adding a new tool = register in `agent/tools/__init__.py::create_default_registry` and the agent can invoke it.
- Vision is available on the underlying models — image content blocks can be returned by tools and the model will reason over them. We haven't exercised this seam yet for tools.

Freeform flow:
- `orchestrator/freeform.py` — promotion/revert helpers (PR-level, post-merge).
- The actual coding happens in the AgentLoop driven by `agent/lifecycle/` modules (planning → coding → review).
- Adding a "verify step" means inserting it as either a new lifecycle phase or as a discipline inside the coding phase.

## The big open questions for the brainstorming skill

These are the decisions that shape what Spec C becomes. Each is a real choice with no obvious right answer — the next agent should ask the user, not assume:

1. **What kinds of projects are we verifying?**
   - Just web apps (front-ends + APIs)?
   - Also CLI tools, libraries, mobile apps?
   - "Web first" is the natural scope but worth pinning explicitly.

2. **Where in the lifecycle does verification happen?**
   - As an internal tool the agent uses opportunistically inside the coding phase ("I made a change, let me check").
   - As a separate explicit "verify" phase between coding and PR creation (the agent MUST verify before opening the PR).
   - As a parallel reviewer that examines the result after coding finishes.
   - Different answers imply very different surface area.

3. **What does "running the project" mean concretely?**
   - Detect the dev-server command (npm/pnpm/yarn run dev, `python run.py`, etc.) from the project's `package.json` / `pyproject.toml` / Procfile / `docker-compose.yml`?
   - User-specified per-repo command in `FreeformConfig`?
   - A new ADR pattern where every repo has a `.auto-agent/run.sh` contract?

4. **Visual feedback mechanism:**
   - Headless Chrome via Playwright (mature, heavyweight, image output).
   - Raw HTTP GETs + DOM inspection (lighter, no visual feedback).
   - Some hybrid: HTTP probe for "is it up", screenshot for "does it look right".
   - How does the screenshot reach the model — `tool_result` with image content block? Saved file + path? Vision-capable model only?

5. **What constitutes "verification passed"?**
   - Server boots + key route returns 200.
   - Screenshot rendered + LLM "looks reasonable" judgment.
   - Comparison against a golden image (where do golden images come from on a fresh feature?).
   - Structured checks the user can declare (e.g. "the page must contain text X after clicking Y").

6. **Process management hygiene:**
   - Dev servers run as long-lived processes. Where do they live (Docker sidecar, subprocess, separate VM)?
   - Port allocation when multiple tasks run concurrently (`MAX_CONCURRENT_TASKS=2` today).
   - Cleanup on agent crash / task timeout — orphan dev servers leaking memory/ports.
   - Time + cost cap on each verification cycle.

7. **Cost / latency budget:**
   - Each screenshot fed to the model burns vision-model tokens.
   - Dev-server boot + screenshot can take 30-60s per cycle.
   - How many verify cycles per task before we give up and ship anyway?

8. **Integration with existing tools:**
   - `test_runner` already exists for headless test execution. Where does this new capability fit relative to it?
   - Does it replace the test-then-ship model or augment it?

## Suggested approach (a starting hypothesis, not a decision)

Two new tools + one new lifecycle phase:

- `agent/tools/dev_server.py` — start/stop/health-check a dev server on an allocated port. Reads a per-repo run command (config first, project-file sniffing fallback).
- `agent/tools/browse_url.py` — Playwright-driven: navigate to URL, return rendered HTML *and* a screenshot (as a tool-result image block). One tool, two outputs.
- New `agent/lifecycle/verify.py` phase after coding, before PR. Spins up the server, probes the routes the agent says were affected, screenshots them, asks the agent to review its own output. Fail → loop back into coding once. Hard cap of N verify cycles.

This is just an opening offer. The brainstorming session should test it against the user's actual constraints — they may want something simpler (just run the dev server and check it boots, no screenshots) or richer (compare against golden images, run E2E flows).

## How to start

1. Read this brief.
2. Read `docs/superpowers/specs/2026-05-12-bigger-po-market-research-design.md` to understand the format and rigor expected (the user reviewed and approved that spec — match its bar).
3. Invoke `superpowers:brainstorming`. The brief above gives you the scope-flag step for free; jump to clarifying questions on the 8 open questions.
4. Output a design spec at `docs/superpowers/specs/2026-05-12-freeform-self-verification-design.md`, then a plan, then execute via subagent-driven development.

## Context the next agent needs about the user

- Personal project. Strongly prefers simplicity; will reject over-engineering.
- Treats the VM (`azureuser@172.190.26.82`) as their dev environment — direct deploys are fine.
- Works in `main` directly, often with multiple agents committing concurrently. Expect to coordinate.
- Reviews specs/plans carefully and pushes back on incoherence (caught a redundant Optional parameter in spec A).
- Wants "work without stopping for clarifying questions" mode — make reasonable calls, ask in batches when you must, don't ping-pong.

## What sub-project A produced (for grounding)

- `agent/market_researcher.py` (inline researcher; runs before PO).
- `MarketBrief` table + Suggestion.evidence_urls + brief_id.
- `agent/tools/web_search.py` + `agent/tools/fetch_url.py` registered via `create_default_registry(with_web=True)`.
- Post-parse filter in `agent/po_analyzer.py::_filter_grounded` drops ungrounded non-bug suggestions.
- `web-next` Suggestion card shows evidence URLs; brief modal off the suggestions page.

The pattern Spec C should follow:
- Mirror the `bigger-po-market-research` spec/plan format.
- TDD throughout (the regression test for "drops ungrounded" was the load-bearing one in A; identify the equivalent here — probably "agent doesn't ship a PR for a feature whose dev server fails to boot").
- Keep concerns isolated: tooling layer (tools/), lifecycle layer (lifecycle/), config (FreeformConfig), UI (web-next/) — same module boundaries.
