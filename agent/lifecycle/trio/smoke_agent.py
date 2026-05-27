"""Smoke agent — dedicated runtime-verification agent. ADR-015 §3 / Phase 7.8.

After the builder finishes an item, a separate LLM-driven agent is
invoked over the same workspace with a focused brief: actually run the
code. The agent installs dependencies, boots the dev server, hits
declared/inferred routes, and/or runs the project's test suite. It
writes its findings to ``.auto-agent/smoke_result.json`` via the
``submit-smoke-result`` skill; the heavy reviewer (and final reviewer)
read that file and treat ``verdict != "pass"`` as a hard fail.

This module exists because the previous reviewer's smoke step had a
vacuous-pass branch: when no routes were declared by the architect and
none could be inferred from the diff, the smoke check was silently
skipped and the item "passed" without any runtime verification. The
classic failure was task 1: 14 items shipped, every per-item review
verdict said ``"smoke": "smoke: no routes declared or inferred"``, the
PR landed, and the code didn't run on the user's machine. ADR-015 §3
says runtime smoke is mandatory; this module makes it so.

The skip-fail rewrite (``"skipped" → "fail"``) is deliberate. ``"skipped"``
is permitted in the skill contract only when the diff is markdown /
docs / comments only, but the orchestrator does not trust the agent to
self-classify the diff. Any agent-emitted ``"skipped"`` is rewritten to
``"fail"`` here so a future agent can't bypass the gate by declaring
the diff doc-only and walking away.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from agent.lifecycle.factory import create_agent
from agent.lifecycle.workspace_paths import SMOKE_RESULT_PATH
from agent.lifecycle.workspace_reader import read_gate_file

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Result dataclass — mirrors the on-disk smoke_result.json shape.
# ---------------------------------------------------------------------------


@dataclass
class SmokeAgentResult:
    """Outcome of one smoke-agent pass.

    ``verdict`` is always either ``"pass"`` or ``"fail"`` after this
    module finishes — the on-disk ``"skipped"`` value is rewritten to
    ``"fail"`` by :func:`run_smoke_agent` because skipping the runtime
    check is the loophole that motivated this whole module.
    """

    verdict: str  # "pass" or "fail"
    summary: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    proposed_smoke_yml: str = ""


# Two-attempt budget for missing-output retry — mirrors the final-reviewer
# pattern in ``trio.final_reviewer._MAX_MISSING_OUTPUT_RETRIES``.
_MAX_MISSING_OUTPUT_RETRIES = 2


# Smoke agents need a hefty turn budget — installing deps + booting +
# exercising routes + running tests can take many bash calls.
_SMOKE_AGENT_MAX_TURNS = 40


SMOKE_AGENT_SYSTEM = """\
You are the smoke-test agent. Your only job is to actually run the
code in this workspace and prove that the diff under review works
end-to-end. You are NOT a code reviewer; you do not judge style or
correctness by reading. You run the code.

You write your verdict to ``.auto-agent/smoke_result.json`` via the
``submit-smoke-result`` skill before stopping. If you do not write the
file, the orchestrator records the run as a hard fail.

Your toolkit:
- Bash — install dependencies, boot the dev server, curl routes, run
  tests, run type-checkers, run build commands.
- Read — inspect the diff, the design doc, package.json / pyproject /
  Makefile to discover how to run this project.

Order of operations (use as many as apply to this diff):

1. **Read ``auto-agent.smoke.yml`` if present.** It tells you the
   project's canonical boot command, health URL, and per-route POST
   bodies / expected response shapes. Use it.
2. **Install dependencies.** ``pip install -e .`` / ``pip install -r
   requirements.txt`` / ``npm install --no-audit --no-fund`` /
   whatever the project uses. A failed install IS a smoke failure.
3. **Run the project's test suite.** ``pytest -q`` / ``npm test`` /
   ``go test ./...``. If the suite previously passed and now fails, the
   change broke something. ALWAYS run this if a test suite exists, even
   when there are routes to exercise — broken tests are smoke
   failures.
4. **Boot the dev server** (when there is one). NEVER run the boot
   command in the foreground — your Bash tool waits for the command to
   exit, and a dev server doesn't exit. You MUST detach it using the
   exact pattern below, or your run will wedge the entire orchestrator
   for an hour (this has happened — see task 28, 2026-05-27).

   Boot template (copy verbatim, replace ``<CMD>`` and ``<PORT>``):
   ```bash
   setsid sh -c '<CMD>' >/tmp/devserver.log 2>&1 </dev/null &
   echo $! > /tmp/devserver.pid
   disown
   # Wait for health — up to 60s — never block longer.
   for i in $(seq 1 60); do
     curl -fsS http://localhost:<PORT>/ >/dev/null 2>&1 && break
     sleep 1
   done
   ```

   ``setsid`` puts the dev server in its own session so closing your
   bash tool doesn't kill it; ``</dev/null`` and ``>/tmp/devserver.log
   2>&1`` detach its stdio so your bash command returns immediately;
   ``disown`` removes it from your shell's job table; the bounded curl
   loop ensures you never wait more than 60s on a stuck boot.
5. **Hit each affected route.** For every route in the work item's
   ``affected_routes`` and every route you can identify in the diff
   (FastAPI / Flask / Next.js page.tsx etc.), curl it. Validate the
   status code is 2xx and the body is not a runtime-stub shape
   (``null``, ``{}``, empty list, ``NotImplementedError`` traceback).
6. **Build / typecheck** for compiled / typed projects. ``tsc
   --noEmit``, ``npm run build``, ``cargo build``.
7. **Kill the dev server BEFORE WRITING smoke_result.json.** This is
   non-negotiable. If you skip the kill, the orchestrator wedges for
   an hour waiting for your claude process to exit, because the dev
   server holds its process group open. Kill template:
   ```bash
   if [ -f /tmp/devserver.pid ]; then
     PID=$(cat /tmp/devserver.pid)
     # Kill the whole session (dev server + any children it spawned).
     kill -TERM -$(ps -o sid= -p "$PID" | tr -d ' ') 2>/dev/null || kill -TERM "$PID" 2>/dev/null
     sleep 2
     kill -KILL -$(ps -o sid= -p "$PID" | tr -d ' ') 2>/dev/null || kill -KILL "$PID" 2>/dev/null
     rm -f /tmp/devserver.pid
   fi
   # Belt-and-suspenders: nothing on the port should remain.
   pkill -KILL -f 'vite|next dev|npm run dev|uvicorn|esbuild' 2>/dev/null || true
   ```
   After running the kill block, verify with
   ``pgrep -af 'vite|next dev|npm run dev|uvicorn|esbuild' || echo clean``.
   If anything is still listed, retry the kill. Do NOT call
   ``submit-smoke-result`` until that check prints ``clean``.

Verdict rules:

- ``"pass"`` — at least one *real* runtime check ran and succeeded
  (boot + route hit, test suite green, or build/typecheck green). The
  diff demonstrably runs.
- ``"fail"`` — a runtime check ran and failed. Set ``failures`` to a
  list of one-line summaries of what broke; include ``output_preview``
  in each attempt so the next coder retry has the stderr to fix from.
- ``"skipped"`` — only legal when the diff is documentation / markdown
  / comments only AND there is no test suite to run. The orchestrator
  rewrites any ``"skipped"`` you emit to ``"fail"`` by default — so if
  you are tempted to skip, run *something* (at minimum, the test
  suite). Reading the code does not count as running it.

Never declare ``"pass"`` without having run at least one shell
command. "I read the code, looks correct" is a fail.
"""


def _smoke_yml_excerpt(workspace_root: str) -> str:
    """Read ``auto-agent.smoke.yml`` from the workspace, if present.

    Returned content is included in the agent's prompt so it doesn't
    need to spend a turn discovering the boot command. Capped at 4KB.
    """

    path = Path(workspace_root) / "auto-agent.smoke.yml"
    if not path.is_file():
        return ""
    try:
        text = path.read_text()
    except OSError:
        return ""
    return text[:4000]


def _build_prompt(
    *,
    item: dict | None,
    design: str,
    diff: str,
    smoke_yml: str,
    workspace_root: str,
) -> str:
    """Compose the smoke agent's user prompt.

    All four context blocks (item / design / diff / smoke.yml) are
    pre-trimmed so the prompt stays within a sane token budget. The
    agent is free to re-read any of them from disk if it needs more.
    """

    item = item or {}
    item_id = item.get("id", "(integration)")
    item_title = item.get("title", "(no title)")
    item_description = item.get("description", "")
    affected_routes = item.get("affected_routes") or []

    design_block = (design or "(no design.md found)").strip()[:4000]
    diff_block = (diff or "(empty diff)").strip()
    if len(diff_block) > 20_000:
        diff_block = diff_block[:20_000] + "\n... (truncated)"

    smoke_yml_block = (
        f"\n== auto-agent.smoke.yml (project canonical) ==\n{smoke_yml}\n"
        if smoke_yml
        else "\n(no auto-agent.smoke.yml present — auto-detect the boot command)\n"
    )

    routes_block = (
        "\n".join(f"- {r}" for r in affected_routes)
        if affected_routes
        else "(none declared — infer from the diff if there are any)"
    )

    return (
        f"== Workspace ==\n{workspace_root}\n\n"
        f"== Work item ({item_id}) ==\n"
        f"Title: {item_title}\n"
        f"Affected routes:\n{routes_block}\n\n"
        f"Description:\n{item_description}\n\n"
        f"== Design context ==\n{design_block}\n"
        f"{smoke_yml_block}\n"
        f"== Diff under verification ==\n```diff\n{diff_block}\n```\n\n"
        "Run the code. Write your verdict to "
        "`.auto-agent/smoke_result.json` via the `submit-smoke-result` "
        "skill, then stop."
    )


def _clear_stale_result(workspace_root: str) -> None:
    """Remove any leftover ``smoke_result.json`` so a previous run's
    verdict can't accidentally satisfy this turn."""

    abs_path = os.path.join(workspace_root, SMOKE_RESULT_PATH)
    if os.path.isfile(abs_path):
        with contextlib.suppress(OSError):
            os.remove(abs_path)


def _parse_smoke_payload(payload: Any) -> SmokeAgentResult:
    """Validate the smoke_result.json payload and synthesise a result.

    Skipped is rewritten to fail (see module docstring). Bad shapes are
    rewritten to fail with a diagnostic summary. ``schema_version``
    mismatches are pre-filtered by :func:`read_gate_file`.
    """

    if not isinstance(payload, dict):
        return SmokeAgentResult(
            verdict="fail",
            summary="smoke_result.json was not a JSON object",
            failures=["smoke_result.json malformed: not a dict"],
        )

    raw_verdict = str(payload.get("verdict", "")).strip().lower()
    summary = str(payload.get("summary", "")).strip()
    attempts_raw = payload.get("attempts") or []
    attempts = [a for a in attempts_raw if isinstance(a, dict)]
    failures_raw = payload.get("failures") or []
    failures = [str(f) for f in failures_raw if isinstance(f, (str, int, float))]
    proposed_smoke_yml = str(payload.get("proposed_smoke_yml", "")).strip()

    if raw_verdict == "pass":
        return SmokeAgentResult(
            verdict="pass",
            summary=summary or "smoke passed",
            attempts=attempts,
            failures=failures,
            proposed_smoke_yml=proposed_smoke_yml,
        )

    if raw_verdict == "skipped":
        # The orchestrator does not trust the agent's self-classification
        # — see module docstring. Rewrite to fail.
        skip_summary = (
            'smoke agent emitted verdict="skipped"; this is rewritten '
            "to fail per ADR-015 §3 — runtime verification is mandatory. "
            f"agent's stated reason: {summary or '(none)'}"
        )
        return SmokeAgentResult(
            verdict="fail",
            summary=skip_summary,
            attempts=attempts,
            failures=[skip_summary, *failures],
            proposed_smoke_yml=proposed_smoke_yml,
        )

    if raw_verdict == "fail":
        return SmokeAgentResult(
            verdict="fail",
            summary=summary or "smoke failed",
            attempts=attempts,
            failures=failures or ["smoke agent reported fail without details"],
            proposed_smoke_yml=proposed_smoke_yml,
        )

    return SmokeAgentResult(
        verdict="fail",
        summary=f"smoke_result.json had unknown verdict={raw_verdict!r}",
        failures=[f"unknown verdict: {raw_verdict!r}"],
    )


async def run_smoke_agent(
    *,
    workspace_root: str,
    item: dict | None,
    design: str,
    diff: str,
    repo_name: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
    task_id: int = 0,
) -> SmokeAgentResult:
    """Run the smoke agent over the workspace and return its verdict.

    Args:
        workspace_root: Absolute path to the workspace where the
            builder's changes are checked out.
        item: The backlog item dict the builder just shipped. ``None``
            when this is the final-reviewer's integration-level smoke
            (no per-item scope).
        design: ``.auto-agent/design.md`` text, for context.
        diff: The unified diff being smoke-tested.
        repo_name / home_dir / org_id / task_id: Standard plumbing
            forwarded to ``create_agent`` — task_id≠0 enables
            heartbeat/streaming.

    Returns:
        :class:`SmokeAgentResult` with ``verdict`` always ∈
        ``{"pass", "fail"}``.

    Behaviour on missing output: the agent gets up to
    ``_MAX_MISSING_OUTPUT_RETRIES`` attempts to write the result file.
    If the file is still missing after the last attempt, returns
    ``verdict="fail"`` with a clear ``summary`` so the dispatcher's
    coder-retry loop has something concrete to feed back.
    """

    _clear_stale_result(workspace_root)
    smoke_yml = _smoke_yml_excerpt(workspace_root)
    prompt = _build_prompt(
        item=item,
        design=design,
        diff=diff,
        smoke_yml=smoke_yml,
        workspace_root=workspace_root,
    )

    last_summary = ""
    for attempt in range(_MAX_MISSING_OUTPUT_RETRIES):
        agent = create_agent(
            workspace=workspace_root,
            task_id=task_id,
            task_description=(f"smoke test for {(item or {}).get('id', 'integration')}")[:200],
            readonly=False,
            with_browser=False,
            max_turns=_SMOKE_AGENT_MAX_TURNS,
            repo_name=repo_name,
            home_dir=home_dir,
            org_id=org_id,
            session=None,  # fresh each invocation — smoke is stateless.
        )
        agent.system_prompt_override = SMOKE_AGENT_SYSTEM

        amend = ""
        if attempt > 0:
            amend = (
                "Your previous turn did not write `.auto-agent/smoke_result.json`. "
                "You MUST call the submit-smoke-result skill before stopping. "
                "Do not summarise in chat — write the file.\n\n"
            )

        try:
            await agent.run(amend + prompt)
        except Exception as exc:  # pragma: no cover — surfaced via summary
            last_summary = f"smoke agent crashed: {exc!r}"
            log.warning(
                "trio.smoke_agent.exception",
                attempt=attempt,
                error=str(exc)[:300],
            )
            continue

        try:
            payload = read_gate_file(
                workspace_root,
                SMOKE_RESULT_PATH,
                schema_version="1",
            )
        except ValueError as exc:
            last_summary = f"smoke_result.json invalid: {exc}"
            log.warning(
                "trio.smoke_agent.bad_payload",
                attempt=attempt,
                error=str(exc)[:300],
            )
            continue

        if payload is not None:
            return _parse_smoke_payload(payload)

        last_summary = "smoke agent did not write .auto-agent/smoke_result.json after its turn"
        log.warning(
            "trio.smoke_agent.missing_output",
            attempt=attempt,
            workspace_root=workspace_root,
        )

    return SmokeAgentResult(
        verdict="fail",
        summary=last_summary or "smoke agent produced no smoke_result.json",
        failures=[last_summary or "smoke_result.json missing after all retries"],
    )


__all__ = [
    "SMOKE_AGENT_SYSTEM",
    "SmokeAgentResult",
    "run_smoke_agent",
]
