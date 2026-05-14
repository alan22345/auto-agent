# [ADR-014] Split the trio decision contract — prose by the architect, structure by a cheap classifier

## Status

Accepted

## Context

The trio lifecycle has four decision points where the orchestrator needs a structured answer from an LLM:

1. **architect.run_initial** — backlog of work items, or a clarification question.
2. **architect.checkpoint** — `done` / `continue` / `revise` / `blocked` / `awaiting_clarification`, plus an optional amended backlog.
3. **dispatcher reviewer** — `{ok, feedback}` verdict on a coder's diff.
4. **dispatcher architect_tiebreak** — `accept` / `redo` / `revise_backlog` / `clarify` after the coder↔reviewer loop didn't converge.

Until this ADR, each point asked the LLM to (a) produce a long reasoning response in prose AND (b) emit a structured JSON envelope at the end. Two attempts at making that contract reliable both failed in production:

**Attempt 1: regex-extract the JSON block at the end of the prose response.** The `_extract_backlog` / `_extract_checkpoint_payload` / `_extract_verdict` regex parsers in `agent/lifecycle/trio/architect.py` and `agent/lifecycle/trio/reviewer.py` looked for a final ` ```json ... ``` ` block. Failure mode: the model spent its turn on the analysis and forgot the envelope. Task 169 (run_initial) hit this on 2026-05-13 — the architect emitted 30+ KB of prose with five numbered questions and no JSON block — and was patched with a one-shot retry prompt nudging the model to emit JSON. Task 170 (checkpoint) hit the same failure on 2026-05-14 at 11:34:44 UTC — the architect emitted 6 KB of markdown "Checkpoint Review" with no JSON, no retry path on `checkpoint`, parent BLOCKED.

**Attempt 2: replace JSON extraction with structured tool calls.** Built `agent/tools/trio_decision.py` with `SubmitBacklogTool` / `SubmitClarificationTool` / `SubmitCheckpointDecisionTool` / `SubmitReviewVerdictTool` / `SubmitTiebreakTool`, each writing into a caller-supplied `DecisionSink`. Wired into `architect.checkpoint` + `architect.run_initial` + `dispatcher._run_reviewer` + `dispatcher.architect_tiebreak`. Failure mode: on the production VM with `LLM_PROVIDER=claude_cli`, `AgentLoop._run_passthrough` short-circuits the agentic loop and the prompt goes straight to `claude --print`. Claude Code uses ITS OWN built-in tool catalogue (Read/Edit/Write/Bash/Grep/Glob) — the Python-side `AgentLoop.tools` registry is bypassed entirely. Confirmed empirically when task 170's re-run on the new code produced an error log saying *"submit_checkpoint_decision and submit_backlog are not in my toolset and not in the deferred-tools list"* — CC introspected its catalogue, didn't find them, blocked the task with `architect.checkpoint.invalid_decision`.

The deeper problem: asking the same model turn to (a) reason at length AND (b) commit to a structured envelope is **two jobs in one prompt**, and the second job is the one that drops. JSON-at-end-of-prose is fragile because nothing in the protocol forces it. Tool-call commitment is robust on Bedrock/Anthropic SDK paths but invisible on the CC pass-through path.

## Decision

**Split the contract: the heavy model reasons in prose; a cheap-and-narrow model classifies the prose into the structured envelope.** Each decision point becomes two LLM calls:

1. **Heavy turn** (the existing architect / reviewer agent on whatever provider is configured — CC pass-through on the prod VM, Bedrock-Sonnet on eval). Produces unconstrained prose. The system prompt asks for clarity, not for JSON.

2. **Extractor call** (always Bedrock + Haiku, regardless of `LLM_PROVIDER`). Takes the heavy turn's text plus an extractor system prompt ("you extract the architect's checkpoint decision from this review"). Returns a narrow JSON object via `agent.llm.structured.complete_json`, which handles fence-stripping + brace-locating + one bounded retry on parse failure.

A new helper `agent/llm/__init__.py::get_structured_extractor_provider()` returns a Bedrock-Haiku provider regardless of `settings.llm_provider`, so the extraction layer is independent of the heavy-turn provider. All four extractors live in `agent/lifecycle/trio/extract.py`:

- `extract_initial_output(text)` → `{"kind": "backlog", "items": [...]}` or `{"kind": "clarification", "question": "..."}`
- `extract_checkpoint_output(text)` → `{"decision": {action, reason, ...}, "backlog": [...] | None}`
- `extract_review_verdict(text)` → `{"ok": bool, "feedback": str}`
- `extract_tiebreak_decision(text)` → `{"action": "accept|redo|revise_backlog|clarify", ...}` (action-conditional required fields)

Call sites collapse from "run agent → regex-extract → fall back to retry prompt" to "run agent → `await extract_X(output)` → branch on the result." If the extractor exhausts its retries (very rare for Haiku on a clear prose response), the caller fails closed — BLOCKED for checkpoint, `clarify` for tiebreak.

System prompts (`ARCHITECT_INITIAL_SYSTEM` / `ARCHITECT_CHECKPOINT_SYSTEM` / `TRIO_REVIEWER_SYSTEM`) updated to ask for plain prose with an explicit final-line statement of intent ("Decision: done — ..."). The "emit JSON at the end" instructions are removed; the "call submit_X tool" instructions from attempt #2 are also removed (they confuse CC, which can't see the tools).

## Consequences

**Easier:**

- The contract becomes "the heavy model reasons clearly; the cheap model produces the schema." Each model does one job. The brittle "remember the envelope at the end of 6 KB of prose" requirement is gone.
- Works in BOTH the CC pass-through path AND the Bedrock-native path identically. The extractor doesn't care which provider the heavy turn used — it only reads its prose output.
- Robust to model drift. If a new Claude version writes its checkpoint reviews differently (more markdown, less markdown, different section ordering), the extractor adapts at the system-prompt layer without us touching the heavy prompt.
- Re-uses an established codebase pattern. `agent/llm/structured.py::complete_json` is what the task classifier, intent extractor, and memory extractor already use for the same kind of structured one-shot. Now five callers, all going through the same seam.
- Tests are simpler: extractor tests mock the cheap provider directly (`agent.lifecycle.trio.extract.get_structured_extractor_provider`), and exercise each extractor's shape validation under happy + malformed cases. 17 new tests in `tests/test_trio_extract.py`.

**Harder:**

- Adds one LLM round-trip per decision point. For a 5-item backlog task, that's ~12 extra Haiku calls (initial + per-item reviewer + per-item tiebreak when needed + final checkpoint). Cost: ~$0.01–$0.02 total per task — rounding error against the architect's Sonnet cost.
- Two-stage pipeline is one more thing to reason about. Mitigated by the extractor being narrow and well-tested.
- The extractor can theoretically mis-classify, but Haiku on a "did the architect say done or revise?" task is reliable. If we ever see a mis-classification in the wild we tighten the extractor system prompt — same iteration loop as a regex fix, but on a more forgiving substrate.
- `agent/tools/trio_decision.py` (the submit_X tools from attempt #2) stays in the tree as dead code on the CC path. Reason to keep: when/if the project moves to Bedrock-native agentic mode, the tools become first-class structured outputs and the extractor becomes a fallback. Cheap to keep, expensive to re-derive.
- The legacy JSON-extract helpers in `architect.py` (`_extract_backlog`, `_extract_checkpoint_payload`, `_extract_clarification`) are now unused on the main paths. The retry-on-missing-JSON path in `run_initial` (commit 59d8249) is also no longer load-bearing. Left in place for now as a defensive third-line fallback if the extractor fails AND the architect produced compliant JSON anyway; revisit for deletion once we've observed the extractor in production.

**Why Haiku, not Sonnet, for the extractor:**

The extractor's job is small — read a few KB of prose, fit a fixed schema. Haiku is fast (~1s typical), cheap (~$0.001 per call), and reliable for this kind of classification. Using Sonnet would burn the cost savings the whole architecture was supposed to capture. The cap on input length (`_MAX_INPUT_CHARS = 24_000`) keeps even the largest analyses inside Haiku's comfort zone.

**Why force Bedrock for the extractor regardless of `LLM_PROVIDER`:**

The whole point is to side-step the CC pass-through's lack of tool-call protocol. If we routed the extractor through CC too, we'd inherit the same brittleness one layer down. Bedrock is always available on the VM (creds are in `.env`) and the extractor is a tight schema-validated call where its API-tool-use support is the load-bearing feature.

## Related

- ADR-010 (structured LLM output) — introduces `agent.llm.structured.complete_json`, the primitive this ADR builds on.
- ADR-013 (trio drives backlog via subagents) — the design this ADR de-bugs. The dispatcher's coder↔reviewer flow worked end-to-end on task 170; only the JSON-extraction contract failed.
- Task 169 incident (2026-05-13) — first observation of the JSON-at-end-of-prose failure mode; patched with a retry prompt.
- Task 170 incident (2026-05-14) — second observation, in `checkpoint` not `run_initial`; motivated this ADR.
- `agent/tools/trio_decision.py` — the abandoned attempt-#2 tool wiring; preserved as dead-but-cheap code for a possible future Bedrock-native return.
- Memory: `claude_cli_no_custom_tools.md` — the lesson that motivated abandoning attempt #2.
