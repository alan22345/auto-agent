# [ADR-008] Collapse `claude_runner/` into `agent/`

## Status

Accepted

## Context

`claude_runner/` and `agent/` had near-identical task lifecycles. Every
handler (`handle_planning`, `handle_coding`, `handle_independent_review`,
`handle_pr_review_comments`, `handle_clarification_response`,
`handle_blocked_response`, `handle_deploy_preview`, `handle_task_cleanup`,
`handle_query`, `handle_harness_onboarding`, `_po_worker`, `event_loop`)
existed in both packages. So did every helper (`get_task`, `get_repo`,
`transition_task`, `_session_id`, `_branch_name`, `_pr_title`,
`_extract_clarification`) and every prompt builder.

CLAUDE.md justified `claude_runner/` as "(legacy) Claude CLI execution
loop — kept for pass-through mode." That justification was already
stale: pass-through is implemented as `agent/llm/claude_cli.py`
(`ClaudeCLIProvider`) and routed via `agent/loop.py::_run_passthrough`
through the same `LLMProvider` seam every other provider uses.
`run.py:76` already imported `agent.main.event_loop`, not the
`claude_runner` one — so the `claude_runner` event loop never even ran
in production.

The only consumer of `claude_runner/` outside the package itself was
`orchestrator/create_repo.py::_generate_name_via_claude`, which shelled
out to the `claude` CLI in a `tempfile.TemporaryDirectory` to pick a
repo slug. The temp dir was a CLI-era artefact: a one-shot prompt has
no need for a cwd.

## Decision

Apply the deletion test: delete `claude_runner/` entirely.

- Reroute `_generate_name_via_claude` through the `LLMProvider` seam
  (`agent.llm.get_provider().complete()`) — same path every other agent
  flow uses.
- Delete `claude_runner/__init__.py`, `main.py`, `harness.py`,
  `po_analyzer.py`, `prompts.py`, `summarizer.py`, `workspace.py`.
- Drop `claude_runner` from `pyproject.toml`'s `known-first-party`
  (replace with `agent`) and from every `module_boundaries` rule in
  `.auto-agent/entropy.yml`.
- Strip the `claude_runner/` row from CLAUDE.md's layer diagram and
  module-boundaries table.
- Rename `run.py`'s `claude_runner_loop` import alias to
  `agent_event_loop`.

The Redis stream consumer name `consumer="claude-runner"` in
`agent/main.py` stays — it is a stable wire-protocol id, not a module
name; renaming it would orphan in-flight stream entries.

## Consequences

**Wins.**
- ~2300 lines of duplicate code deleted. Every fix to `agent/` no longer
  has to be remembered for `claude_runner/`.
- The repo slug generator no longer requires the `claude` CLI to be
  installed on production VMs; it uses Bedrock by default like every
  other LLM call.
- One source of truth for the task lifecycle. The `agent/` versions are
  strictly deeper: `agent/main.py:593 generate_repo_summary` uses
  readonly tools instead of opaque CLI stdout; `agent/workspace.py`
  adds `EmptyBranchError`, `commit_pending_changes`,
  `ensure_branch_has_commits`, `fallback_branch` handling, and agent
  git identity.

**Trade-offs.**
- The pure CLI pass-through path now requires the `claude_cli`
  `LLMProvider` to be selected via configuration. There is no separate
  package that bypasses the agent loop entirely. This is intentional —
  the loop's `_run_passthrough` already short-circuits the agentic
  loop for CLI providers, so the behaviour is preserved.

**Alternatives rejected.**
- *Keep `claude_runner/` as a thin facade over `agent/`.* That is the
  shallow-wrapper anti-pattern — every module should stand on its own.
  A facade with no behaviour of its own forces every reader to chase
  through it to the real implementation.
- *Move `claude_runner/` into `agent/legacy/`.* Same problem: dead code
  pretending to be live.
