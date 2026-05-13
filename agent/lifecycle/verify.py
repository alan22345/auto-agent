"""Verify lifecycle phase — boot check + intent check.

Runs between CODING and PR_CREATED (state ``VERIFYING``). Two cycles max:
fail cycle 1 → CODING; fail cycle 2 → BLOCKED.
"""
from __future__ import annotations

import asyncio
import re as _re
from datetime import UTC, datetime

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
    """Entry point: run the verify phase for a task currently in VERIFYING."""
    task = await get_task(task_id)
    if not task:
        return
    cycle = await _next_cycle(task_id)
    if cycle > MAX_VERIFY_CYCLES:
        log.error(f"task #{task_id}: verify cycle budget exhausted before start")
        await transition_task(task_id, "blocked", "verify_failed: budget exhausted")
        return

    await publish(verify_started(task_id, cycle))
    attempt = await _create_verify_attempt(task_id, cycle)

    try:
        await asyncio.wait_for(
            _run_verify_body(task, task_id, cycle, attempt),
            timeout=PHASE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        await _fail_cycle(task_id, attempt, cycle, "phase_timeout", None)


async def _run_verify_body(task, task_id: int, cycle: int, attempt) -> None:
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
            except _dev_server.BootTimeout as e:
                await _update_verify_attempt(attempt.id, boot_check="fail", log_tail=e.log_tail)
                return await _fail_cycle(task_id, attempt, cycle, "boot_timeout", e.log_tail)
            except _dev_server.EarlyExit as e:
                await _update_verify_attempt(attempt.id, boot_check="fail", log_tail=e.log_tail)
                return await _fail_cycle(task_id, attempt, cycle, "early_exit", e.log_tail)
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
            return await _fail_cycle(task_id, attempt, cycle, "intent_not_addressed", None)
        return await _pass_cycle(task_id, attempt, task, workspace, base_branch)
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
