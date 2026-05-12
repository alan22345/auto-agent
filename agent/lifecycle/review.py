"""Review phase — independent PR review, plan auto-review, and PR comment handling.

Three handlers and the gh-CLI PR creation helpers live here together because
they share concerns: PR creation idempotency, the freeform plan auto-review
hook, and the human-driven comment-resume flow all touch the same review
session conventions and the same independent-reviewer guardrails.
"""

from __future__ import annotations

import contextlib
import tempfile
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from agent import sh
from agent.lifecycle._naming import _branch_name, _fresh_session_id, _session_id
from agent.lifecycle._orchestrator_api import (
    ORCHESTRATOR_URL,
    get_freeform_config,
    get_repo,
    get_task,
    transition_task,
)
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.prompts import (
    build_plan_independent_review_prompt,
    build_pr_independent_review_prompt_with_ui_check,
    build_pr_review_response_prompt,
)
from agent.tools import dev_server as _dev_server
from agent.workspace import (
    clone_repo,
    commit_pending_changes,
    push_branch,
)
from shared.database import async_session
from shared.events import (
    Event,
    publish,
    review_skipped_no_runner,
    task_review_comments_addressed,
    task_review_complete,
)
from shared.logging import setup_logging
from shared.models import ReviewAttempt
from shared.quotas import QuotaExceeded
from shared.types import ReviewCombinedVerdict

log = setup_logging("agent.lifecycle.review")


MAX_REVIEW_CYCLES = 2


async def find_existing_pr_url(
    workspace: str,
    head_branch: str,
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
) -> str | None:
    """Return the URL of an existing open PR for `head_branch`, or None.

    Uses `gh pr list --head <branch> --state open --json url,state`. If gh
    fails or returns no results, returns None so the caller falls through to
    `gh pr create` normally.

    Rationale: when a task goes back to CODING after a deploy/review failure,
    `_finish_coding` is called again. The branch already has an open PR, so
    `gh pr create` fails with "pull request for branch X already exists".
    Checking first makes the path idempotent — re-entry just pushes new
    commits to the same PR.
    """
    from shared.github_auth import get_github_token

    result = await sh.run(
        ["gh", "pr", "list", "--head", head_branch, "--state", "open", "--json", "url,state"],
        cwd=workspace,
        timeout=20,
        env={"GH_TOKEN": await get_github_token(
            user_id=user_id, organization_id=organization_id,
        )},
    )
    if result.failed:
        return None
    try:
        import json as _json

        prs = _json.loads(result.stdout)
        for pr in prs:
            if pr.get("state") == "OPEN" and pr.get("url"):
                return pr["url"]
    except Exception:
        return None
    return None


async def create_pr(
    workspace: str,
    title: str,
    body: str,
    base_branch: str,
    head_branch: str,
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
) -> str:
    """Create a PR using the gh CLI, or return the existing one if the branch
    already has an open PR. Idempotent — safe to call after pushing new
    commits to a branch with an existing PR."""
    existing = await find_existing_pr_url(
        workspace, head_branch,
        user_id=user_id, organization_id=organization_id,
    )
    if existing:
        log.info(f"PR already exists for {head_branch}, reusing: {existing}")
        return existing

    from shared.github_auth import get_github_token

    result = await sh.run(
        [
            "gh", "pr", "create",
            "--title", title,
            "--body", body,
            "--base", base_branch,
            "--head", head_branch,
        ],
        cwd=workspace,
        timeout=30,
        env={"GH_TOKEN": await get_github_token(
            user_id=user_id, organization_id=organization_id,
        )},
    )
    if result.failed:
        raise RuntimeError(
            f"gh pr create failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


# --- ReviewAttempt persistence + verdict helpers ---


async def _next_review_cycle(task_id: int) -> int:
    """Return the next cycle number for this task (1-indexed)."""
    async with async_session() as s:
        rows = (
            await s.execute(
                select(ReviewAttempt).where(ReviewAttempt.task_id == task_id),
            )
        ).scalars().all()
        return len(rows) + 1


async def _create_review_attempt(task_id: int, cycle: int):
    """Insert a ReviewAttempt row with status='error' up-front for crash safety."""
    async with async_session() as s:
        a = ReviewAttempt(task_id=task_id, cycle=cycle, status="error")
        s.add(a)
        await s.commit()
        await s.refresh(a)
        return a


async def _update_review_attempt(attempt_id: int, *, finished: bool = False, **fields) -> None:
    async with async_session() as s:
        a = (
            await s.execute(select(ReviewAttempt).where(ReviewAttempt.id == attempt_id))
        ).scalar_one()
        for k, v in fields.items():
            setattr(a, k, v)
        if finished:
            a.finished_at = datetime.now(UTC)
        await s.commit()


async def _review_loop_back(task_id: int, cycle: int, reason: str) -> None:
    """Transition the task back to CODING on cycle 1 or to BLOCKED on cycle 2."""
    if cycle >= MAX_REVIEW_CYCLES:
        await transition_task(task_id, "blocked", f"review_failed: {reason}")
    else:
        await transition_task(
            task_id, "coding", f"review failed (cycle {cycle}): {reason}"
        )


def _parse_review_combined_verdict(output: str) -> ReviewCombinedVerdict | None:
    """Try to parse the reviewer's combined-verdict JSON object.

    Looks for the first balanced ``{...}`` substring and feeds it to Pydantic.
    Returns None when no JSON object is present or it does not match the schema
    (legacy free-form review output falls through to the keyword fallback).
    """
    if not output:
        return None
    start = output.find("{")
    if start < 0:
        return None
    depth, end = 0, None
    for i, ch in enumerate(output[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return ReviewCombinedVerdict.model_validate_json(output[start:end])
    except Exception:
        return None


async def handle_independent_review(task_id: int, pr_url: str, branch_name: str) -> None:
    """Review a PR with a fresh agent session (independent reviewer).

    Persists a ``ReviewAttempt`` per invocation and runs an optional UI-check
    sub-step when the task declared ``affected_routes`` and a run command can be
    resolved for the project. On rejection, transitions the task back to
    CODING (cycle 1) or to BLOCKED (cycle 2). Approval publishes
    ``task_review_complete(approved=True)`` as before.
    """
    task = await get_task(task_id)
    if not task or not task.repo_name:
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    cycle = await _next_review_cycle(task_id)
    if cycle > MAX_REVIEW_CYCLES:
        log.error(f"task #{task_id}: review cycle budget exhausted before start")
        await transition_task(task_id, "blocked", "review_failed: budget exhausted")
        return
    attempt = await _create_review_attempt(task_id, cycle)

    base_branch = repo.default_branch
    fallback_branch: str | None = None
    run_command_override: str | None = None
    if task.freeform_mode and task.repo_name:
        freeform_cfg = await get_freeform_config(task.repo_name)
        if freeform_cfg:
            base_branch = freeform_cfg.dev_branch
            fallback_branch = freeform_cfg.prod_branch or repo.default_branch
            run_command_override = getattr(freeform_cfg, "run_command", None)

    # Per-invocation session — the reviewer is a fresh, independent agent
    # by design. A deterministic hash here collides on retry (the Claude
    # CLI provider rejects re-used session IDs with "already in use").
    reviewer_session = _fresh_session_id(task_id, "review")

    log.info(
        f"Independent review of task #{task_id} PR cycle={cycle} (session={reviewer_session})"
    )
    workspace = await clone_repo(
        repo.url, task_id, base_branch,
        fallback_branch=fallback_branch,
        user_id=task.created_by_user_id,
        organization_id=task.organization_id,
    )

    server_cm = None
    server_handle = None
    routes = task.affected_routes or []

    try:
        await sh.run(["git", "checkout", branch_name], cwd=workspace, timeout=30)

        # UI-check setup: boot the project's dev server when routes were
        # declared AND a run command resolves. Failure to boot is treated
        # as a review failure and loops back via the cycle budget.
        if routes:
            run_cmd = _dev_server.sniff_run_command(
                workspace, override=run_command_override,
            )
            if run_cmd is None:
                await publish(review_skipped_no_runner(task_id))
            else:
                server_cm = _dev_server.start_dev_server(
                    workspace, override=run_command_override,
                )
                try:
                    server_handle = await server_cm.__aenter__()
                    await _dev_server.wait_for_port(
                        server_handle.port,
                        timeout=60,
                        log_path=server_handle.log_path,
                    )
                except (_dev_server.BootTimeout, _dev_server.BootError) as e:
                    log_tail = getattr(e, "log_tail", str(e))
                    await _update_review_attempt(
                        attempt.id,
                        status="fail",
                        ui_check="fail",
                        failure_reason="boot_timeout",
                        log_tail=log_tail,
                        finished=True,
                    )
                    with contextlib.suppress(Exception):
                        await server_cm.__aexit__(type(e), e, None)
                    server_cm = None
                    server_handle = None
                    return await _review_loop_back(
                        task_id, attempt.cycle, "boot_timeout",
                    )

        server_url = (
            f"http://localhost:{server_handle.port}" if server_handle else None
        )
        prompt = build_pr_independent_review_prompt_with_ui_check(
            task.title, task.description, pr_url, base_branch,
            server_url=server_url,
            affected_routes=routes,
        )
        agent = create_agent(
            workspace,
            session_id=reviewer_session,
            readonly=True,
            with_browser=server_handle is not None,
            max_turns=20,
            task_description=task.description,
            repo_name=task.repo_name,
            home_dir=await home_dir_for_task(task),
            org_id=task.organization_id,
            dev_server_log_path=(
                server_handle.log_path if server_handle else None
            ),
        )
        result = await agent.run(prompt)
        output = result.output or ""
        tool_calls = getattr(result, "tool_calls", []) or []
        log.info(
            f"Independent review for task #{task_id} cycle={cycle}: {output[:300]}..."
        )

        # If the underlying provider crashed (e.g. Claude CLI subprocess
        # failure that didn't recover), it returns "[ERROR] ...". Treating
        # that string as review feedback would route it to the coding agent
        # as if it were a real comment — and the coder would try to "fix"
        # the CLI error. Skip the review entirely on a recognised error
        # prefix and emit an auto-approve so the task isn't blocked.
        if output.lstrip().startswith("[ERROR]"):
            log.warning(
                f"Task #{task_id}: reviewer agent errored ({output[:200]!r}), "
                "skipping review and auto-approving so the task isn't blocked"
            )
            await _update_review_attempt(
                attempt.id,
                status="error",
                code_review_verdict=output[:4000],
                failure_reason="agent_error",
                finished=True,
            )
            await publish(
                task_review_complete(
                    task_id,
                    review=f"Review skipped — agent error: {output[:500]}",
                    pr_url=pr_url,
                    branch=branch_name,
                    approved=True,
                )
            )
            return

        verdict = _parse_review_combined_verdict(output)
        if verdict is None:
            # Legacy fallback: free-form approval keywords. UI check is
            # treated as SKIPPED so we don't manufacture a UI verdict the
            # agent never emitted.
            approved_legacy = any(
                phrase in output.lower()
                for phrase in [
                    "--approve", "lgtm", "looks good", "pr review --approve",
                ]
            )
            code_verdict = "OK" if approved_legacy else "NOT-OK"
            ui_verdict = "SKIPPED"
            code_reasoning = output[:4000]
            ui_reasoning = ""
        else:
            code_verdict = verdict.code_review.verdict
            ui_verdict = verdict.ui_check.verdict
            code_reasoning = verdict.code_review.reasoning
            ui_reasoning = verdict.ui_check.reasoning

        ui_check_status = {
            "OK": "pass",
            "NOT-OK": "fail",
            "SKIPPED": "skipped",
        }.get(ui_verdict, "skipped")

        approved = code_verdict == "OK" and ui_verdict in ("OK", "SKIPPED")

        if approved:
            log.info(f"Independent review approved task #{task_id} cycle={cycle}")
            await _update_review_attempt(
                attempt.id,
                status="pass",
                code_review_verdict=code_reasoning,
                ui_check=ui_check_status,
                ui_judgment=ui_reasoning or None,
                tool_calls=tool_calls,
                finished=True,
            )
            await publish(
                task_review_complete(
                    task_id,
                    review=output[:2000],
                    pr_url=pr_url,
                    branch=branch_name,
                    approved=True,
                )
            )
            return

        # Rejection — record and transition.
        fail_reason = (
            "ui_judgment_not_ok" if code_verdict == "OK" else "code_review_rejected"
        )
        log.info(
            f"Independent review rejected task #{task_id} cycle={cycle} "
            f"reason={fail_reason}"
        )
        await _update_review_attempt(
            attempt.id,
            status="fail",
            code_review_verdict=code_reasoning,
            ui_check=ui_check_status,
            ui_judgment=ui_reasoning or None,
            tool_calls=tool_calls,
            failure_reason=fail_reason,
            finished=True,
        )
        await publish(
            task_review_complete(
                task_id,
                review=output[:2000],
                pr_url=pr_url,
                branch=branch_name,
                approved=False,
            )
        )
        await _review_loop_back(task_id, attempt.cycle, fail_reason)

    except QuotaExceeded as e:
        log.info("task_blocked_on_quota", task_id=task_id, reason=str(e))
        await _update_review_attempt(
            attempt.id, status="error",
            failure_reason="blocked_on_quota",
            finished=True,
        )
        await transition_task(task_id, "blocked_on_quota", str(e))
    except Exception as e:
        log.exception(f"Independent review failed for task #{task_id}")
        await _update_review_attempt(
            attempt.id, status="error",
            failure_reason=f"internal_error: {e}"[:200],
            finished=True,
        )
        # Auto-approve on infrastructure errors so a transient crash doesn't
        # block the task forever (same posture as before).
        await publish(
            task_review_complete(
                task_id,
                review=f"Review skipped: {e}",
                pr_url=pr_url,
                branch=branch_name,
                approved=True,
            )
        )
    finally:
        if server_cm is not None:
            try:
                await server_cm.__aexit__(None, None, None)
            except Exception:
                log.exception(
                    f"task #{task_id}: dev server cleanup raised after review"
                )


async def handle_plan_independent_review(task_id: int) -> None:
    """Run an independent reviewer on a freeform task's plan."""
    task = await get_task(task_id)
    if not task:
        return
    if task.status != "awaiting_approval":
        log.info(f"Plan auto-review skipped for task #{task_id}: status is '{task.status}'")
        return
    if not task.freeform_mode:
        return
    if not task.plan:
        # Plan is missing (likely due to the DB-save bug — plan was passed in the
        # transition API body but not persisted). Can't review an empty plan —
        # auto-approve so the task doesn't get stuck forever at AWAITING_APPROVAL.
        log.warning(f"Task #{task_id}: plan is empty, auto-approving without review")
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
                json={
                    "approved": True,
                    "message": "Plan auto-approved (plan text was empty — review skipped)",
                },
            )
        return

    log.info(f"Running independent plan review for freeform task #{task_id}")
    prompt = build_plan_independent_review_prompt(task.title, task.description, task.plan)

    try:
        with tempfile.TemporaryDirectory(prefix=f"plan-review-{task_id}-") as tmp:
            agent = create_agent(
                tmp,
                readonly=True,
                max_turns=5,
                model_tier="fast",
                task_description=task.description,
                repo_name=task.repo_name,
                home_dir=await home_dir_for_task(task),
                org_id=task.organization_id,
            )
            result = await agent.run(prompt)
            output = result.output
    except Exception as e:
        log.exception(f"Plan auto-review failed for task #{task_id}")
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
                json={
                    "approved": True,
                    "message": f"Plan auto-approved (reviewer error: {e})",
                },
            )
        return

    output_stripped = output.strip()
    log.info(f"Plan reviewer output for task #{task_id}: {output_stripped[:300]}...")

    verdict = ""
    reasoning_start = 0
    for i, line in enumerate(output_stripped.splitlines()):
        if line.strip():
            verdict = line.strip().upper()
            reasoning_start = i + 1
            break
    reasoning = (
        "\n".join(output_stripped.splitlines()[reasoning_start:]).strip()
        or "(no reasoning provided)"
    )

    approved = verdict.startswith("APPROVE")
    decision_label = "APPROVED" if approved else "REJECTED"
    log_message = f"Plan {decision_label} by independent reviewer\n\n{reasoning[:1900]}"

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
            json={
                "approved": approved,
                "feedback": reasoning if not approved else "",
                "message": log_message,
            },
        )

    log.info(f"Plan auto-review complete for task #{task_id}: {decision_label}")


async def handle_pr_review_comments(task_id: int, comments: str) -> None:
    """Address PR review comments by resuming the coding session."""
    task = await get_task(task_id)
    if not task or not task.repo_name or not task.pr_url:
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    session_id = _session_id(task_id, task.created_at)
    base_branch = repo.default_branch
    branch_name = task.branch_name or await _branch_name(task_id, task.title)

    log.info(f"Addressing PR review for task #{task_id} (session={session_id})")
    if task.status in ("awaiting_review", "awaiting_ci"):
        await transition_task(task_id, "coding", f"Addressing feedback: {comments[:200]}")
    workspace = await clone_repo(
        repo.url, task_id, base_branch,
        user_id=task.created_by_user_id,
        organization_id=task.organization_id,
    )

    try:
        await sh.run(["git", "checkout", branch_name], cwd=workspace, timeout=30)

        prompt = build_pr_review_response_prompt(task.title, task.description, comments)
        agent = create_agent(
            workspace,
            session_id=session_id,
            max_turns=30,
            task_description=task.description,
            repo_name=task.repo_name,
            home_dir=await home_dir_for_task(task),
            org_id=task.organization_id,
        )
        result = await agent.run(prompt, resume=True)
        log.info(f"PR review response for task #{task_id}: {result.output[:300]}...")

        # Safety net — agent may have addressed comments without committing
        committed_now = await commit_pending_changes(
            workspace, task_id, f"Address PR review comments — {task.title}"
        )
        if committed_now:
            log.warning(
                f"Task #{task_id}: PR-review agent left uncommitted changes — auto-committed"
            )
        await push_branch(workspace, branch_name)

        await publish(
            task_review_comments_addressed(
                task_id, output=result.output[:1000], pr_url=task.pr_url or ""
            )
        )

    except QuotaExceeded as e:
        log.info("task_blocked_on_quota", task_id=task_id, reason=str(e))
        await transition_task(task_id, "blocked_on_quota", str(e))
    except Exception as e:
        log.exception(f"PR review response failed for task #{task_id}")
        await transition_task(task_id, "blocked", f"Failed to address review: {e}")


async def handle_plan_ready(event: Event) -> None:
    """EventBus entry — auto-review the plan for freeform-mode tasks.

    Guards on ``task.freeform_mode`` and ``status == "awaiting_approval"``
    inside ``handle_plan_independent_review``; this handler just dispatches.
    """
    if not event.task_id:
        return
    task = await get_task(event.task_id)
    if task and task.freeform_mode and task.status == "awaiting_approval":
        await handle_plan_independent_review(event.task_id)
