"""Verify lifecycle phase — boot check + intent check.

Runs between CODING and PR_CREATED (state ``VERIFYING``). Two cycles max:
fail cycle 1 → CODING; fail cycle 2 → BLOCKED.

ADR-015 §5/§11 Phase 5 adds :func:`run_verify_primitives_for_task` — the
complex-flow gate that runs the shared verify primitives end-to-end on
the working-tree diff and persists ``.auto-agent/smoke_result.json``.
The legacy ``handle_verify`` continues to drive the freeform
self-verification path; both share the same primitive layer now so a
regression on one can't slip past the other.
"""
from __future__ import annotations

import asyncio
import json
import os
import re as _re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from agent import sh
from agent.lifecycle._naming import _fresh_session_id
from agent.lifecycle._orchestrator_api import (
    get_freeform_config,
    get_repo,
    get_task,
    transition_task,
)
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.route_inference import (
    infer_routes_from_diff,
    is_ui_route,
)
from agent.lifecycle.verify_primitives import (
    RouteResult,
    ServerHandle,
    StubResult,
    UIResult,
    Violation,
    boot_dev_server,
    exercise_routes,
    grep_diff_for_stubs,
    inspect_ui,
)
from agent.lifecycle.workspace_paths import AUTO_AGENT_DIR, SMOKE_RESULT_PATH
from agent.prompts import build_verify_intent_prompt
from agent.tools import dev_server as _dev_server
from agent.workspace import clone_repo
from shared.database import async_session
from shared.events import (
    publish,
    verify_failed,
    verify_passed,
    verify_skipped_no_runner,
    verify_started,
)
from shared.logging import setup_logging
from shared.models import VerifyAttempt
from shared.types import IntentVerdict

log = setup_logging("agent.lifecycle.verify")

MAX_VERIFY_CYCLES = 2
PHASE_TIMEOUT_SECONDS = 120
HOLD_SECONDS = 5

_INTENT_OK_RE = _re.compile(r"^\s*OK\s*$")
_INTENT_NOTOK_RE = _re.compile(r"^\s*NOT-OK\b", _re.IGNORECASE)


async def handle_verify(task_id: int) -> None:
    """Entry point: run the verify phase for a task currently in VERIFYING.

    ADR-015 §5/§11 Phase 5 — complex (non-trio, non-freeform) tasks
    delegate to :func:`run_verify_primitives_for_task`, which runs the
    four shared verify primitives end-to-end and writes
    ``.auto-agent/smoke_result.json``. Freeform tasks keep the legacy
    boot+intent flow until Phase 10 reshapes freeform handling.
    """
    task = await get_task(task_id)
    if not task:
        return

    # Complex-flow short-circuit: run the shared primitives directly.
    is_complex = (
        getattr(task, "complexity", None) == "complex"
        and not getattr(task, "freeform_mode", False)
        and not getattr(task, "parent_task_id", None)
    )
    if is_complex:
        await _handle_verify_complex(task_id, task)
        return

    cycle = await _next_cycle(task_id)
    if cycle > MAX_VERIFY_CYCLES:
        log.error(f"task #{task_id}: verify cycle budget exhausted before start")
        await transition_task(task_id, "blocked", "verify_failed: budget exhausted")
        return

    await publish(verify_started(task_id, cycle))
    attempt = await _create_verify_attempt(task_id, cycle)

    # wrap only boot + intent stages in the timeout envelope;
    # PR-creation handoff (_pass_cycle / _open_pr_and_advance) runs outside
    # so a hanging `gh pr create` cannot trigger cancellation mid-network-call.
    try:
        pass_args = await asyncio.wait_for(
            _run_boot_and_intent(task, task_id, cycle, attempt),
            timeout=PHASE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        await _fail_cycle(task_id, attempt, cycle, "phase_timeout", None)
        return

    if pass_args is not None:
        workspace, base_branch = pass_args
        await _pass_cycle(task_id, attempt, task, workspace, base_branch)


async def _run_boot_and_intent(
    task, task_id: int, cycle: int, attempt,
) -> tuple[str, str] | None:
    """Run boot check + intent check inside the timeout envelope.

    Returns ``(workspace, base_branch)`` when the cycle passes so that
    ``handle_verify`` can call ``_pass_cycle`` *outside* the timeout envelope
    (avoids cancelling a ``gh pr create`` mid-network-call).
    Returns ``None`` when the cycle fails (fail path already handled).
    """
    workspace, base_branch = await _prepare_workspace(task)
    override = await _resolve_run_command_override(task)
    run_cmd = _dev_server.sniff_run_command(workspace, override=override)

    server_cm = None
    server = None
    try:
        if run_cmd:
            server_cm = _dev_server.start_dev_server(workspace, override=override)
            try:
                server = await server_cm.__aenter__()
                await _dev_server.wait_for_port(server.port, timeout=60, log_path=server.log_path)
                await _dev_server.hold(server, seconds=HOLD_SECONDS)
                await _update_verify_attempt(attempt.id, boot_check="pass")
            except _dev_server.BootError as e:
                await _update_verify_attempt(attempt.id, boot_check="fail", log_tail=str(e))
                await _fail_cycle(task_id, attempt, cycle, "boot_error", str(e))
                return None
            except _dev_server.BootTimeout as e:
                await _update_verify_attempt(attempt.id, boot_check="fail", log_tail=e.log_tail)
                await _fail_cycle(task_id, attempt, cycle, "boot_timeout", e.log_tail)
                return None
            except _dev_server.EarlyExit as e:
                await _update_verify_attempt(attempt.id, boot_check="fail", log_tail=e.log_tail)
                await _fail_cycle(task_id, attempt, cycle, "early_exit", e.log_tail)
                return None
        else:
            await _update_verify_attempt(attempt.id, boot_check="skipped")
            if task.affected_routes or []:
                await publish(verify_skipped_no_runner(task_id))

        # Intent check
        verdict = await run_intent_check(task, workspace, server)
        await _update_verify_attempt(
            attempt.id,
            intent_check="pass" if verdict.ok else "fail",
            intent_judgment=verdict.reasoning,
            tool_calls=verdict.tool_calls,
        )
        if not verdict.ok:
            await _fail_cycle(task_id, attempt, cycle, "intent_not_addressed", None)
            return None
        return workspace, base_branch
    finally:
        if server_cm is not None:
            try:
                await server_cm.__aexit__(None, None, None)
            except Exception:
                log.exception(f"task #{task_id}: dev server cleanup raised")


async def run_intent_check(task, workspace: str, server) -> IntentVerdict:
    """Single readonly-agent invocation; agent emits OK or NOT-OK on its first line."""
    diff_result = await sh.run(
        ["git", "diff", "--stat", "HEAD~1"],
        cwd=workspace,
        timeout=30,
    )
    diff_summary = (diff_result.stdout or "").strip() or "(no diff stat available)"

    server_url = f"http://localhost:{server.port}" if server is not None else None
    prompt = build_verify_intent_prompt(
        task.title,
        task.description,
        diff_summary,
        task.affected_routes or [],
        server_url,
    )

    session_id = _fresh_session_id(task.id, "verify-intent")
    agent = create_agent(
        workspace,
        session_id=session_id,
        readonly=True,
        with_browser=server is not None,
        max_turns=15,
        task_description=task.description,
        repo_name=task.repo_name,
        home_dir=await home_dir_for_task(task),
        org_id=task.organization_id,
        dev_server_log_path=(server.log_path if server is not None else None),
    )
    result = await agent.run(prompt)
    output = (result.output or "").strip()

    first_line = output.splitlines()[0] if output else ""
    if _INTENT_OK_RE.match(first_line):
        ok = True
    elif _INTENT_NOTOK_RE.match(first_line):
        ok = False
    else:
        # Malformed first line → fail closed (treat as NOT-OK).
        ok = False
    return IntentVerdict(
        ok=ok,
        reasoning=output[:4000],
        tool_calls=getattr(result, "tool_calls", []) or [],
    )


async def _pass_cycle(task_id: int, attempt, task, workspace: str, base_branch: str) -> None:
    await _update_verify_attempt(attempt.id, status="pass", finished=True)
    await publish(verify_passed(task_id, attempt.cycle))

    # Trio children take a detour through TRIO_REVIEW before PR creation —
    # the reviewer is the alignment gate between builder output and the
    # architect's intent in ARCHITECTURE.md. Non-trio tasks go straight to
    # _open_pr_and_advance (existing behaviour).
    if getattr(task, "parent_task_id", None):
        await transition_task(
            task_id, "trio_review", "verify passed; dispatching trio reviewer",
        )
        from agent.lifecycle.trio.reviewer import handle_trio_review
        # Fire-and-forget so verify returns; the reviewer is responsible
        # for transitioning the task to PR_CREATED (then coding's
        # _open_pr_and_advance is invoked) or back to CODING.
        asyncio.create_task(  # noqa: RUF006 — fire-and-forget hand-off
            handle_trio_review(
                task_id, workspace=workspace, parent_branch=base_branch,
            )
        )
        return

    from agent.lifecycle.coding import _open_pr_and_advance
    branch_name = task.branch_name
    if not branch_name:
        raise RuntimeError(f"task #{task_id}: task.branch_name missing — cannot push")
    await _open_pr_and_advance(task_id, task, workspace, base_branch, branch_name)


async def _fail_cycle(
    task_id: int, attempt, cycle: int, reason: str, log_tail: str | None,
) -> None:
    await _update_verify_attempt(
        attempt.id, status="fail", finished=True,
        failure_reason=reason, log_tail=log_tail,
    )
    await publish(verify_failed(task_id, cycle, reason))
    if cycle >= MAX_VERIFY_CYCLES:
        await transition_task(task_id, "blocked", f"verify_failed: {reason}")
    else:
        await transition_task(task_id, "coding", f"verify failed (cycle {cycle}): {reason}")


async def _resolve_run_command_override(task) -> str | None:
    if getattr(task, "freeform_mode", False) and task.repo_name:
        cfg = await get_freeform_config(task.repo_name)
        if cfg and getattr(cfg, "run_command", None):
            return cfg.run_command
    return None


async def _prepare_workspace(task) -> tuple[str, str]:
    repo = await get_repo(task.repo_name)
    base_branch = repo.default_branch
    if task.freeform_mode and task.repo_name:
        cfg = await get_freeform_config(task.repo_name)
        if cfg:
            base_branch = cfg.dev_branch
    workspace = await clone_repo(
        repo.url, task.id, base_branch,
        user_id=task.created_by_user_id,
        organization_id=task.organization_id,
    )
    return workspace, base_branch


# --- DB helpers ---

async def _next_cycle(task_id: int) -> int:
    async with async_session() as s:
        result = await s.execute(
            select(VerifyAttempt).where(VerifyAttempt.task_id == task_id),
        )
        return len(result.scalars().all()) + 1


async def _create_verify_attempt(task_id: int, cycle: int):
    async with async_session() as s:
        a = VerifyAttempt(task_id=task_id, cycle=cycle, status="error")
        s.add(a)
        await s.commit()
        await s.refresh(a)
        return a


async def _update_verify_attempt(attempt_id: int, finished: bool = False, **fields) -> None:
    async with async_session() as s:
        a = (await s.execute(
            select(VerifyAttempt).where(VerifyAttempt.id == attempt_id),
        )).scalar_one()
        for k, v in fields.items():
            setattr(a, k, v)
        if finished:
            a.finished_at = datetime.now(UTC)
        await s.commit()


# ---------------------------------------------------------------------------
# ADR-015 §5/§11 Phase 5 — complex-flow verify primitives integration.
# ---------------------------------------------------------------------------


async def _handle_verify_complex(task_id: int, task) -> None:
    """Drive the complex-flow verify primitives gate.

    Mirrors the boot+intent path's outer shape: prepare the workspace,
    run the gate inside the timeout envelope, and on pass hand off to
    ``_open_pr_and_advance``. Attempt number is derived from the count
    of prior VerifyAttempt rows so a re-entry after a CODING retry sees
    ``attempt=2`` and escalates to BLOCKED on a second failure (the
    1-retry bound from ADR-015 §5 Phase 5).
    """

    attempt_n = await _next_cycle(task_id)
    await publish(verify_started(task_id, attempt_n))
    attempt = await _create_verify_attempt(task_id, attempt_n)
    try:
        workspace, base_branch = await _prepare_workspace(task)
        task.base_branch = base_branch
        result = await asyncio.wait_for(
            run_verify_primitives_for_task(
                task=task, workspace_root=workspace, attempt=attempt_n,
            ),
            timeout=PHASE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        await _update_verify_attempt(
            attempt.id, status="fail", failure_reason="phase_timeout", finished=True,
        )
        await publish(verify_failed(task_id, attempt_n, "phase_timeout"))
        if attempt_n >= MAX_PRIMITIVE_ATTEMPTS:
            await transition_task(task_id, "blocked", "verify primitives phase_timeout")
        else:
            await transition_task(task_id, "coding", "verify primitives phase_timeout — retrying")
        return

    if not result.ok:
        await _update_verify_attempt(
            attempt.id, status="fail",
            failure_reason=result.reason or "primitive_failure",
            finished=True,
        )
        await publish(verify_failed(task_id, attempt_n, result.reason or "primitive_failure"))
        # Transition was already done inside run_verify_primitives_for_task.
        return

    await _update_verify_attempt(attempt.id, status="pass", finished=True)
    await publish(verify_passed(task_id, attempt_n))

    branch_name = getattr(task, "branch_name", None)
    if not branch_name:
        log.error("verify_complex_missing_branch", task_id=task_id)
        await transition_task(
            task_id, "blocked",
            "verify primitives passed but task.branch_name missing",
        )
        return
    from agent.lifecycle.coding import _open_pr_and_advance
    await _open_pr_and_advance(task_id, task, workspace, base_branch, branch_name)


@dataclass
class VerifyPrimitivesResult:
    """Outcome of one run of the four shared verify primitives.

    The dataclass shape mirrors what's written to
    ``.auto-agent/smoke_result.json`` minus the schema version.
    """

    ok: bool
    violations: list[Violation]
    route_results: dict[str, RouteResult]
    ui_results: dict[str, UIResult]
    reason: str = ""


# 1-retry budget per ADR-015 §5 Phase 5: first failure ⇒ CODING (retry);
# second failure ⇒ BLOCKED. Module-level so tests can introspect.
MAX_PRIMITIVE_ATTEMPTS = 2


async def _load_diff(workspace_root: str, *, base_branch: str = "main") -> str:
    """Return the working-tree diff vs ``base_branch``.

    The complex flow runs this before the PR is opened, so the diff
    surface is whatever the builder committed in the task's branch
    relative to its base. Falls back to ``HEAD~1..HEAD`` when the base
    isn't available locally (shallow clone).
    """

    result = await sh.run(
        ["git", "diff", f"{base_branch}...HEAD"],
        cwd=workspace_root,
        timeout=30,
    )
    if not result.failed and result.stdout.strip():
        return result.stdout
    fallback = await sh.run(
        ["git", "diff", "HEAD~1..HEAD"],
        cwd=workspace_root,
        timeout=30,
    )
    return fallback.stdout if not fallback.failed else ""


def _write_smoke_result(workspace_root: str, result: VerifyPrimitivesResult) -> None:
    """Persist the verify-primitives result to ``.auto-agent/smoke_result.json``.

    Always called — pass or fail — so the orchestrator (and any human
    looking at the workspace) has a single artefact describing what the
    gate saw.
    """

    os.makedirs(os.path.join(workspace_root, AUTO_AGENT_DIR), exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": "1",
        "ok": result.ok,
        "reason": result.reason,
        "violations": [
            {
                "file": v.file,
                "line": v.line,
                "pattern": v.pattern,
                "snippet": v.snippet,
                "allowed_via_optout": v.allowed_via_optout,
            }
            for v in result.violations
        ],
        "routes": {
            r: {"ok": rr.ok, "status": rr.status, "reason": rr.reason}
            for r, rr in result.route_results.items()
        },
        "ui": {
            r: {"ok": ur.ok, "reason": ur.reason}
            for r, ur in result.ui_results.items()
        },
    }
    target = os.path.join(workspace_root, SMOKE_RESULT_PATH)
    with open(target, "w") as fh:
        json.dump(payload, fh, indent=2)


async def run_verify_primitives_for_task(
    *,
    task: Any,
    workspace_root: str,
    attempt: int = 1,
) -> VerifyPrimitivesResult:
    """Run the shared verify primitives end-to-end against the working diff.

    Order is fixed (and pinned by tests):

      1. ``grep_diff_for_stubs`` — no-defer guard.
      2. ``boot_dev_server`` — only when the diff inferred routes.
      3. ``exercise_routes`` — same handle.
      4. ``inspect_ui`` — every route classified as UI by
         :func:`route_inference.is_ui_route` that returned 2xx.

    On any failure, transition the task back to CODING (attempt 1) or
    BLOCKED (attempt 2). Always writes ``.auto-agent/smoke_result.json``.
    """

    base_branch = getattr(task, "base_branch", None) or "main"
    diff = await _load_diff(workspace_root, base_branch=base_branch)

    # --- Layer 1: stub grep -------------------------------------------------
    stub_result: StubResult = grep_diff_for_stubs(diff)
    blocking = [v for v in stub_result.violations if not v.allowed_via_optout]
    if blocking:
        result = VerifyPrimitivesResult(
            ok=False,
            violations=blocking,
            route_results={},
            ui_results={},
            reason="stub_violations",
        )
        _write_smoke_result(workspace_root, result)
        await _route_primitives_failure(
            task=task, attempt=attempt, reason="stub_violations",
        )
        return result

    # --- Layer 2: route exercise -------------------------------------------
    routes = infer_routes_from_diff(diff)
    route_results: dict[str, RouteResult] = {}
    ui_results: dict[str, UIResult] = {}

    handle: ServerHandle | None = None
    try:
        if routes:
            handle = await boot_dev_server(workspace=workspace_root)
            if handle.state == "running":
                route_results = await exercise_routes(routes, handle=handle)
            elif handle.state == "failed":
                result = VerifyPrimitivesResult(
                    ok=False,
                    violations=[],
                    route_results={},
                    ui_results={},
                    reason=f"boot_failed:{handle.failure_reason or 'unknown'}",
                )
                _write_smoke_result(workspace_root, result)
                await _route_primitives_failure(
                    task=task, attempt=attempt, reason=result.reason,
                )
                return result

            # --- Layer 3: UI inspection (advisory on missing playwright) ----
            if handle.state == "running":
                for route, rr in route_results.items():
                    if not is_ui_route(route) or not rr.ok:
                        continue
                    ui = await inspect_ui(
                        route=route,
                        intent=(
                            getattr(task, "description", "")
                            or getattr(task, "title", "")
                        ),
                        base_url=handle.base_url,
                    )
                    ui_results[route] = ui
    finally:
        if handle is not None:
            await handle.teardown()

    failing_routes = [r for r, rr in route_results.items() if not rr.ok]
    failing_ui = [
        r
        for r, ur in ui_results.items()
        if not ur.ok and "playwright_not_installed" not in ur.reason
    ]

    if failing_routes or failing_ui:
        reason = (
            "route_failures"
            if failing_routes
            else "ui_inspection_failed"
        )
        result = VerifyPrimitivesResult(
            ok=False,
            violations=[],
            route_results=route_results,
            ui_results=ui_results,
            reason=reason,
        )
        _write_smoke_result(workspace_root, result)
        await _route_primitives_failure(task=task, attempt=attempt, reason=reason)
        return result

    result = VerifyPrimitivesResult(
        ok=True,
        violations=[],
        route_results=route_results,
        ui_results=ui_results,
        reason="",
    )
    _write_smoke_result(workspace_root, result)
    return result


async def _route_primitives_failure(
    *,
    task: Any,
    attempt: int,
    reason: str,
) -> None:
    """Transition the task on a primitives-gate failure.

    Attempt 1 ⇒ CODING (one-retry); attempt 2+ ⇒ BLOCKED.
    """

    task_id = getattr(task, "id", None)
    if task_id is None:
        return
    if attempt >= MAX_PRIMITIVE_ATTEMPTS:
        await transition_task(
            task_id,
            "blocked",
            f"verify primitives failed twice: {reason}",
        )
        return
    await transition_task(
        task_id,
        "coding",
        f"verify primitives failed (attempt {attempt}): {reason} — retrying",
    )
