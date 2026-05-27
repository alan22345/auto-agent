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
You are the smoke-test agent. Your job is to verify the diff under
review actually runs. You are NOT a code reviewer; you do not judge
style or correctness by reading. You exercise the code.

**Dev-server lifecycle is owned by auto-agent, not by you.** Before
this turn the orchestrator already booted the project's dev server,
curled the affected routes, and tore it down. The route check results
are in your prompt below — treat them as ground truth. DO NOT run
``npm run dev``, ``vite``, ``next dev``, ``uvicorn``, ``python3
run.py``, ``make dev`` or any other long-lived foreground command:
your Bash tool waits for the command to exit, dev servers don't exit,
and the whole orchestrator wedges for an hour. If your bash tool
blocks more than ~15s on a single command, you've made a mistake —
kill it (Ctrl-C / kill the process) and run something else.

You write your verdict to ``.auto-agent/smoke_result.json`` via the
``submit-smoke-result`` skill before stopping. If you do not write the
file, the orchestrator records the run as a hard fail.

Your toolkit:
- Bash — install dependencies, run the test suite, run type-checkers,
  run build commands. **Only run commands that exit on their own.**
- Read — inspect the diff, the design doc, package.json / pyproject /
  Makefile to understand the project.

Order of operations (use as many as apply to this diff):

1. **Read the auto-agent route-check section in this prompt.** If the
   orchestrator booted the server and any route returned non-2xx or a
   runtime-stub shape (null/{}/empty-list/NotImplementedError), that
   IS a smoke failure — record it in ``failures`` and you may stop
   after running the test suite. If routes are all 2xx, runtime
   correctness for HTTP surfaces is already proven; move on to tests.
2. **Install dependencies if needed.** ``pip install -e .`` / ``pip
   install -r requirements.txt`` / ``npm install --no-audit
   --no-fund``. A failed install IS a smoke failure. Skip if already
   installed (idempotent).
3. **Run the project's test suite.** ``pytest -q`` / ``npm test`` /
   ``go test ./...``. ALWAYS run this if a test suite exists — broken
   tests are smoke failures. The test suite exits on its own; safe to
   run in the foreground.
4. **Build / typecheck** for compiled / typed projects. ``tsc
   --noEmit``, ``npm run build``, ``cargo build``. Also safe in
   foreground — they exit.

Verdict rules:

- ``"pass"`` — at least one *real* runtime check ran and succeeded
  (auto-agent's route checks all 2xx, test suite green, or
  build/typecheck green). The diff demonstrably runs.
- ``"fail"`` — a runtime check ran and failed: auto-agent's route
  checks include a non-2xx or runtime-stub response, the test suite
  failed, or build/typecheck failed. Set ``failures`` to a list of
  one-line summaries of what broke; include ``output_preview`` in each
  attempt so the next coder retry has the stderr to fix from.
- ``"skipped"`` — only legal when the diff is documentation / markdown
  / comments only AND there is no test suite to run. The orchestrator
  rewrites any ``"skipped"`` you emit to ``"fail"`` by default — so if
  you are tempted to skip, run *something* (at minimum, the test
  suite). Reading the code does not count as running it.

Never declare ``"pass"`` without either (a) reading auto-agent's route
checks and finding them green, or (b) running at least one shell
command (tests / build / typecheck). "I read the code, looks correct"
is a fail.
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
    route_check_block: str,
) -> str:
    """Compose the smoke agent's user prompt.

    All context blocks (item / design / diff / smoke.yml / route checks)
    are pre-trimmed so the prompt stays within a sane token budget. The
    agent is free to re-read any of them from disk if it needs more.

    ``route_check_block`` is the auto-agent-owned boot+curl summary —
    embedded here so the agent doesn't need (and isn't allowed) to
    boot dev servers itself. See module docstring.
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
        else "\n(no auto-agent.smoke.yml present)\n"
    )

    routes_block = (
        "\n".join(f"- {r}" for r in affected_routes)
        if affected_routes
        else "(none declared on the work item)"
    )

    return (
        f"== Workspace ==\n{workspace_root}\n\n"
        f"== Work item ({item_id}) ==\n"
        f"Title: {item_title}\n"
        f"Declared affected routes:\n{routes_block}\n\n"
        f"Description:\n{item_description}\n\n"
        f"== Auto-agent route checks ==\n{route_check_block}\n\n"
        f"== Design context ==\n{design_block}\n"
        f"{smoke_yml_block}\n"
        f"== Diff under verification ==\n```diff\n{diff_block}\n```\n\n"
        "Verify the code. Run tests / build / typecheck as applicable. "
        "Do NOT boot dev servers — auto-agent has already done that and "
        "the results are above. Write your verdict to "
        "`.auto-agent/smoke_result.json` via the `submit-smoke-result` "
        "skill, then stop."
    )


# ---------------------------------------------------------------------------
# Auto-agent-owned dev-server lifecycle (replaces the LLM-driven boot/curl
# /kill that wedged claude when the dev server didn't exit). The smoke
# primitives in ``agent.lifecycle.verify_primitives`` already use
# ``preexec_fn=os.setsid`` + ``killpg`` teardown — we just call them here
# and embed the results in the prompt so claude only needs to interpret.
# ---------------------------------------------------------------------------


_AUTO_ROUTE_CAP = 20  # don't curl more than this many routes per run


async def _run_auto_route_checks(
    *,
    workspace_root: str,
    item: dict | None,
    diff: str,
    repo_id: int | None,
) -> tuple[str, list[str]]:
    """Boot the dev server, curl declared + inferred routes, tear down.

    Returns ``(prompt_block, failures)``. ``prompt_block`` is the
    human-readable section embedded in the smoke-agent prompt;
    ``failures`` is a list of one-line failure summaries the caller
    can use to short-circuit to ``verdict="fail"`` without invoking
    claude (e.g. when the server fails to boot).
    """

    # Late imports — verify_primitives pulls in httpx/yaml/SQLAlchemy
    # which we don't want at module-import time for the unit tests.
    from agent.lifecycle.route_inference import infer_routes_from_diff
    from agent.lifecycle.verify_primitives import boot_dev_server, exercise_routes

    declared = list((item or {}).get("affected_routes") or [])
    inferred = list(infer_routes_from_diff(diff or ""))
    seen: set[str] = set()
    routes: list[str] = []
    for r in declared + inferred:
        if r and r not in seen:
            routes.append(r)
            seen.add(r)
        if len(routes) >= _AUTO_ROUTE_CAP:
            break

    smoke_yml_path = Path(workspace_root) / "auto-agent.smoke.yml"
    has_smoke_yml = smoke_yml_path.is_file()

    if not routes and not has_smoke_yml:
        return (
            "(no routes declared, none inferable from the diff, no "
            "auto-agent.smoke.yml — auto-agent did not boot a dev "
            "server. Runtime correctness for this diff hinges on the "
            "test suite / build / typecheck below.)",
            [],
        )

    handle = None
    try:
        handle = await boot_dev_server(workspace=workspace_root, repo_id=repo_id)

        if handle.state == "disabled":
            return (
                "(auto-agent could not find a boot command "
                "[auto-agent.smoke.yml absent and no package.json `dev` "
                "script / run.py / Makefile `dev` target detected]. "
                "No dev server was started. Verify via tests / build / "
                "typecheck below.)",
                [],
            )

        if handle.state == "failed":
            reason = handle.failure_reason or "boot or health probe failed"
            failure = f"dev server failed to boot: {reason}"
            return (
                f"AUTO-AGENT BOOT FAILED: {reason}. This is a smoke "
                "failure independent of whatever you check below.",
                [failure],
            )

        if not routes:
            return (
                f"Dev server booted at {handle.base_url} but no routes "
                "to curl (no work-item affected_routes, none inferred "
                "from diff). Verify via tests / build / typecheck.",
                [],
            )

        results = await exercise_routes(routes, handle=handle)

        failures: list[str] = []
        lines: list[str] = [f"Dev server booted at {handle.base_url}."]
        for route, rr in results.items():
            tag = "OK" if rr.ok else "FAIL"
            body_preview = (rr.body or "").strip()[:200].replace("\n", " ")
            line = f"  [{tag}] {route} → status={rr.status}"
            if not rr.ok:
                line += f" reason={rr.reason}"
                failures.append(f"route {route} returned status={rr.status} ({rr.reason})")
            if body_preview:
                line += f" body={body_preview!r}"
            lines.append(line)
        return ("\n".join(lines), failures)
    except Exception as exc:
        log.warning(
            "trio.smoke_agent.auto_route_checks_crashed",
            error=str(exc)[:300],
        )
        # A crash in the primitives mustn't break the smoke flow —
        # claude can still run tests/build/typecheck. Surface the crash
        # in the prompt but don't synthesise a hard failure.
        return (
            f"(auto-agent route check crashed: {exc!r}. Boot/curl was "
            "not completed; rely on tests/build/typecheck below.)",
            [],
        )
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                await handle.teardown()


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
    repo_id: int | None = None,
    task_id: int = 0,
) -> SmokeAgentResult:
    """Run the smoke agent over the workspace and return its verdict.

    Two phases:

    1. **Auto-agent route checks** (Python-side, no LLM). Boot the dev
       server via ``boot_dev_server``, curl declared + inferred routes
       via ``exercise_routes``, tear down via ``ServerHandle.teardown``.
       This owns the dev-server lifecycle — the LLM is forbidden from
       running long-lived foreground commands because claude's Bash
       tool blocks until the command exits, wedging the orchestrator
       (task 28, 2026-05-27).
    2. **LLM verifier** (claude). Receives the route-check results in
       the prompt and is responsible only for commands that exit:
       installing deps, running the test suite, build/typecheck. Writes
       the final verdict to ``.auto-agent/smoke_result.json``.

    If the dev server fails to boot AND there are routes to check, we
    short-circuit to ``verdict="fail"`` without invoking claude — a
    boot failure is definitive.

    Args:
        workspace_root: Absolute path to the workspace where the
            builder's changes are checked out.
        item: The backlog item dict the builder just shipped. ``None``
            when this is the final-reviewer's integration-level smoke.
        design: ``.auto-agent/design.md`` text, for context.
        diff: The unified diff being smoke-tested.
        repo_name / home_dir / org_id / repo_id / task_id: Standard
            plumbing. ``repo_id`` is required to inject project secrets
            into the dev-server env (ADR-019 §6); when ``None`` the
            server boots without them.

    Returns:
        :class:`SmokeAgentResult` with ``verdict`` always ∈
        ``{"pass", "fail"}``.
    """

    _clear_stale_result(workspace_root)
    smoke_yml = _smoke_yml_excerpt(workspace_root)

    route_check_block, route_failures = await _run_auto_route_checks(
        workspace_root=workspace_root,
        item=item,
        diff=diff,
        repo_id=repo_id,
    )

    if route_failures:
        # A definitive boot/curl failure short-circuits the LLM step:
        # the diff demonstrably does not run, no test-suite victory can
        # change that. Return now so the coder retry loop sees a clear
        # failure reason.
        return SmokeAgentResult(
            verdict="fail",
            summary=route_failures[0][:200],
            failures=route_failures,
        )

    prompt = _build_prompt(
        item=item,
        design=design,
        diff=diff,
        smoke_yml=smoke_yml,
        workspace_root=workspace_root,
        route_check_block=route_check_block,
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
