"""Auto-agent — single entrypoint that starts everything.

Usage:
    python run.py           # local dev
    docker compose up       # containerised (same image)

Starts on port 2020:
    - Web UI at /
    - API at /api
    - Webhooks at /api/webhooks/*
    - Health at /health
"""

from __future__ import annotations

import asyncio
import contextlib
import re as _re
from pathlib import Path

import httpx

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from shared.config import settings
from shared.database import async_session, engine
from shared.events import Event, EventBus
from shared.logging import setup_logging
from shared.models import (
    Base,
    FreeformConfig,
    Repo,
    Suggestion,
    SuggestionStatus,
    Task,
    TaskComplexity,
    TaskHistory,
    TaskSource,
    TaskStatus,
    intake_qa_for_suggestion,
)
from sqlalchemy import select as sa_select
from shared.notifier import send_telegram
from shared.redis_client import (
    ack_event,
    ensure_stream_group,
    get_redis,
    publish_event,
    read_events,
)

from datetime import datetime, timezone

from orchestrator.classifier import classify_task
from orchestrator.metrics import router as metrics_router
from orchestrator.queue import can_start, next_queued_task
from orchestrator.repo_sync import match_repo, sync_repos
from orchestrator.router import router as api_router
from orchestrator.scheduler import run_scheduler
from orchestrator.search import router as search_router
from orchestrator.state_machine import get_task, transition
from orchestrator.webhooks.github import router as github_webhook_router
from orchestrator.webhooks.linear import router as linear_webhook_router

from web.main import (
    websocket_endpoint,
    event_listener as web_event_listener,
)
from integrations.telegram.main import (
    inbound_loop as telegram_inbound_loop,
    notification_loop as telegram_notification_loop,
)
from agent.architect_analyzer import run_architecture_loop
from agent.main import event_loop as claude_runner_loop
from agent.po_analyzer import run_po_analysis_loop

log = setup_logging("auto-agent")


# ---------------------------------------------------------------------------
# Unified FastAPI app — API + webhooks + web UI, all on one port
# ---------------------------------------------------------------------------

app = FastAPI(title="Auto-Agent", version="0.1.0")


# ---------------------------------------------------------------------------
# Auth middleware
#
# JWT-based auth for protected endpoints. Exempts:
#   - /health (deploy health check)
#   - /api/webhooks/* (GitHub/Linear webhooks)
#   - /api/auth/login (login endpoint)
#   - / and /static/* (served to browser, login happens client-side)
#   - Loopback requests from inside the same container
# ---------------------------------------------------------------------------

_AUTH_EXEMPT_PREFIXES = ("/health", "/api/webhooks/", "/api/auth/login", "/api/auth/logout", "/static/")
_AUTH_EXEMPT_EXACT = ("/", "/api/auth/login", "/api/auth/logout")


def _is_auth_exempt(path: str) -> bool:
    if path in _AUTH_EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES)


@app.middleware("http")
async def jwt_auth_middleware(request, call_next):
    if _is_auth_exempt(request.url.path):
        return await call_next(request)

    # Allow internal HTTP calls between services running in the same container.
    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "::1"):
        return await call_next(request)

    from orchestrator.auth import verify_token
    from orchestrator.router import COOKIE_NAME

    # Check for Bearer token
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        payload = verify_token(auth[7:])
        if payload:
            return await call_next(request)

    # Check for session cookie
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        payload = verify_token(cookie)
        if payload:
            return await call_next(request)

    return Response(
        status_code=401,
        content="Authentication required",
    )


# API
app.include_router(api_router, prefix="/api")
app.include_router(metrics_router, prefix="/api")
app.include_router(search_router, prefix="/api")

# Webhooks
app.include_router(github_webhook_router, prefix="/api")
app.include_router(linear_webhook_router, prefix="/api")

# Static files for web UI
STATIC_DIR = Path(__file__).parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text())


@app.websocket("/ws")
async def ws_proxy(ws: WebSocket) -> None:
    # JWT auth is handled inside websocket_endpoint (via ?token= query param)
    await websocket_endpoint(ws)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Orchestrator event handlers
# ---------------------------------------------------------------------------


async def on_task_created(event: Event) -> None:
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return

        task = await transition(session, task, TaskStatus.CLASSIFYING, "Auto-classifying")
        await session.commit()

        # Classify the task (unless pre-set at creation)
        if task.complexity is None:
            complexity, classification = classify_task(task.title, task.description)
            task.complexity = complexity
            await session.commit()
            payload = {"complexity": complexity.value, **classification.model_dump()}
        else:
            complexity = task.complexity
            log.info(f"Task #{task.id} pre-classified as {complexity.value}, skipping classifier")
            payload = {"complexity": complexity.value, "reasoning": "Pre-classified at creation"}

        # SIMPLE_NO_CODE tasks don't need a repo — skip matching and go
        # directly to the query handler.
        if complexity == TaskComplexity.SIMPLE_NO_CODE:
            log.info(f"Task #{task.id} classified as simple_no_code — skipping repo match")
            r = await get_redis()
            await publish_event(r, Event(
                type="task.classified",
                task_id=task.id,
                payload=payload,
            ).to_redis())
            await r.aclose()
            return

        # Auto-match repo from task text if not already set
        if not task.repo_id:
            repo = await match_repo(session, f"{task.title} {task.description}")
            if repo:
                task.repo_id = repo.id
                await session.commit()
                log.info(f"Auto-matched repo '{repo.name}' for task #{task.id}")

        r = await get_redis()
        await publish_event(r, Event(
            type="task.classified",
            task_id=task.id,
            payload=payload,
        ).to_redis())
        await r.aclose()


async def on_task_classified(event: Event) -> None:
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return

        # SIMPLE_NO_CODE: query/research tasks bypass the coding pipeline.
        # They don't need a repo, don't need a slot — just run a single LLM
        # call and complete immediately.
        if task.complexity == TaskComplexity.SIMPLE_NO_CODE:
            task = await transition(session, task, TaskStatus.QUEUED)
            task = await transition(session, task, TaskStatus.CODING, "Processing query...")
            await session.commit()
            r = await get_redis()
            await publish_event(
                r, Event(type="task.query", task_id=task.id).to_redis()
            )
            await r.aclose()
            return

        # Freeform tasks with auto_start_tasks bypass the concurrency queue
        force_start = False
        if task.freeform_mode and task.repo_id:
            from sqlalchemy import select as _sel
            cfg_result = await session.execute(
                _sel(FreeformConfig).where(FreeformConfig.repo_id == task.repo_id)
            )
            cfg = cfg_result.scalar_one_or_none()
            if cfg and cfg.enabled and cfg.auto_start_tasks:
                force_start = True

        if force_start or await can_start(session, task.complexity):
            if task.complexity in (TaskComplexity.COMPLEX, TaskComplexity.COMPLEX_LARGE):
                task = await transition(session, task, TaskStatus.QUEUED)
                task = await transition(session, task, TaskStatus.PLANNING, "Starting planning phase")
            else:
                task = await transition(session, task, TaskStatus.QUEUED)
                task = await transition(session, task, TaskStatus.CODING, "Starting coding")
        else:
            task = await transition(session, task, TaskStatus.QUEUED, "Waiting for available slot")
        await session.commit()

        r = await get_redis()
        if task.status == TaskStatus.PLANNING:
            await publish_event(r, Event(type="task.start_planning", task_id=task.id).to_redis())
        elif task.status == TaskStatus.CODING:
            await publish_event(r, Event(type="task.start_coding", task_id=task.id).to_redis())
        await r.aclose()


async def on_clarification_resolved(event: Event) -> None:
    """After a clarification is answered and Claude continued, resume the task phase."""
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return

        # Determine which phase to resume based on complexity and prior state
        if task.complexity == TaskComplexity.COMPLEX and task.plan is None:
            # Was in planning phase, hasn't produced a plan yet — resume planning
            task = await transition(session, task, TaskStatus.PLANNING, "Clarification resolved, resuming planning")
            await session.commit()
            r = await get_redis()
            await publish_event(r, Event(type="task.start_planning", task_id=task.id).to_redis())
            await r.aclose()
        else:
            # Was in coding phase — resume coding
            task = await transition(session, task, TaskStatus.CODING, "Clarification resolved, resuming coding")
            await session.commit()
            r = await get_redis()
            await publish_event(r, Event(type="task.start_coding", task_id=task.id).to_redis())
            await r.aclose()


async def on_task_approved(event: Event) -> None:
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return
        r = await get_redis()
        await publish_event(r, Event(type="task.start_coding", task_id=task.id).to_redis())
        await r.aclose()


async def on_review_complete(event: Event) -> None:
    """Independent review finished — transition to PR_CREATED → AWAITING_CI."""
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return
        pr_url = event.payload.get("pr_url", "")
        review = event.payload.get("review", "")
        task.pr_url = pr_url
        task = await transition(session, task, TaskStatus.PR_CREATED, f"PR created: {pr_url}")
        task = await transition(
            session, task, TaskStatus.AWAITING_CI,
            f"Independent review complete. Waiting for CI.\n\n{review[:2000]}",
        )
        await session.commit()


async def on_review_comments_addressed(event: Event) -> None:
    """Review feedback addressed — re-enter CI pipeline so deployment is triggered."""
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return
        task = await transition(session, task, TaskStatus.PR_CREATED, "Review feedback addressed, new changes pushed")
        task = await transition(session, task, TaskStatus.AWAITING_CI, "Waiting for CI on updated code")
        await session.commit()


def _should_auto_merge(task, repo_freeform_config) -> bool:
    """Decide whether a task with passing CI should auto-merge to dev.

    Only freeform tasks auto-merge after CI passes. Non-freeform tasks
    always go through human review, even if the repo has a dev branch.

    If no freeform config exists for the repo, fall through to human review.
    """
    if repo_freeform_config is None:
        return False
    if not task.freeform_mode:
        return False
    return bool(getattr(repo_freeform_config, "dev_branch", ""))


async def on_ci_passed(event: Event) -> None:
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return

        # Look up the repo's current freeform config (not the task's snapshot flag)
        repo_freeform_config = None
        if task.repo_id:
            from sqlalchemy import select as _sel
            from shared.models import FreeformConfig as _FC
            result = await session.execute(
                _sel(_FC).where(_FC.repo_id == task.repo_id)
            )
            repo_freeform_config = result.scalar_one_or_none()

        if _should_auto_merge(task, repo_freeform_config):
            # Freeform mode: auto-merge to dev, skip human review
            merge_ok = await _auto_merge_pr(task)
            if merge_ok:
                task = await transition(session, task, TaskStatus.AWAITING_REVIEW, "CI passed, auto-merging to dev")
                task = await transition(session, task, TaskStatus.DONE, "Auto-merged to dev branch")
                await session.commit()
                await _try_start_queued(session)
            else:
                task = await transition(session, task, TaskStatus.AWAITING_REVIEW, "CI passed, auto-merge failed — awaiting manual review")
                await session.commit()
            return

        # Either the task isn't freeform OR the repo's freeform config is
        # disabled. Fall through to human review — this is the safety path.
        if task.freeform_mode and not (repo_freeform_config and repo_freeform_config.enabled):
            log.warning(
                f"Task #{task.id} has freeform_mode=True but repo freeform is disabled — "
                f"falling through to human review (safety gate)"
            )

        task = await transition(session, task, TaskStatus.AWAITING_REVIEW, "CI passed, awaiting human review")
        await session.commit()

    # Trigger dev deploy so the user can review a live preview
    r = await get_redis()
    await publish_event(
        r, Event(type="task.deploy_preview", task_id=event.task_id).to_redis()
    )
    await r.aclose()


# Sentinel strings _fetch_failed_ci_logs returns when it couldn't surface a
# real failure log. _ci_logs_are_empty lets the deploy-failure handler know
# the response carries no actionable diagnostic, so it can fall back to the
# deploy-script output instead of feeding the empty sentinel to the retry loop.
_EMPTY_CI_LOG_SENTINELS: frozenset[str] = frozenset({
    "No failed check runs found",
    "Could not fetch PR details",
    "Could not fetch check runs",
})


def _ci_logs_are_empty(ci_logs: str) -> bool:
    """Return True iff _fetch_failed_ci_logs returned an empty/sentinel value."""
    return ci_logs.strip() in _EMPTY_CI_LOG_SENTINELS


async def _fetch_failed_ci_logs(pr_url: str, token: str) -> str:
    """Fetch the failed GitHub Actions job logs for a PR.

    Returns the log output (truncated) or one of ``_EMPTY_CI_LOG_SENTINELS``
    when there's nothing actionable to surface. Callers should check
    ``_ci_logs_are_empty`` and fall back to deploy-script output before
    feeding a sentinel string to the retry loop.
    """
    try:
        import httpx as _httpx
        parts = pr_url.rstrip("/").split("/")
        owner, repo, pr_number = parts[-4], parts[-3], parts[-1]

        async with _httpx.AsyncClient() as client:
            headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

            # Get PR head SHA
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=headers,
            )
            if resp.status_code != 200:
                return "Could not fetch PR details"
            head_sha = resp.json()["head"]["sha"]

            # Get check runs for this commit
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                headers=headers,
            )
            if resp.status_code != 200:
                return "Could not fetch check runs"

            check_runs = resp.json().get("check_runs", [])
            failed_runs = [
                cr for cr in check_runs
                if cr.get("conclusion") in ("failure", "timed_out")
            ]

            if not failed_runs:
                return "No failed check runs found"

            logs_parts = []
            for cr in failed_runs[:3]:  # Limit to 3 failed runs
                run_id = cr.get("id")
                name = cr.get("name", "unknown")

                # Try to get the run log via the Actions API
                # check_run has details_url or we can get annotations
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/check-runs/{run_id}/annotations",
                    headers=headers,
                )
                if resp.status_code == 200:
                    annotations = resp.json()
                    if annotations:
                        ann_text = "\n".join(
                            f"  {a.get('path', '')}:{a.get('start_line', '')}: {a.get('message', '')}"
                            for a in annotations[:20]
                        )
                        logs_parts.append(f"## Failed: {name}\n{ann_text}")
                        continue

                # Fallback: try to get workflow run logs
                # The check_run has an external_id that may be the job ID
                # Get the workflow run via check_suite
                suite_url = cr.get("check_suite", {}).get("url")
                if suite_url:
                    resp = await client.get(suite_url, headers=headers)
                    if resp.status_code == 200:
                        suite = resp.json()
                        # Get jobs for this workflow run
                        run_url = suite.get("url", "").replace("/check-suites/", "/actions/runs/").rsplit("/", 1)[0]
                        # Actually use the check_suite -> workflow run relationship
                        head_branch = suite.get("head_branch", "")
                        # Try listing workflow runs for this SHA
                        resp = await client.get(
                            f"https://api.github.com/repos/{owner}/{repo}/actions/runs",
                            headers=headers,
                            params={"head_sha": head_sha, "status": "completed", "per_page": 5},
                        )
                        if resp.status_code == 200:
                            runs = resp.json().get("workflow_runs", [])
                            for wf_run in runs:
                                if wf_run.get("conclusion") == "failure":
                                    jobs_url = wf_run.get("jobs_url")
                                    if jobs_url:
                                        resp = await client.get(jobs_url, headers=headers)
                                        if resp.status_code == 200:
                                            jobs = resp.json().get("jobs", [])
                                            for job in jobs:
                                                if job.get("conclusion") == "failure":
                                                    steps = job.get("steps", [])
                                                    failed_steps = [
                                                        s for s in steps
                                                        if s.get("conclusion") == "failure"
                                                    ]
                                                    step_info = "\n".join(
                                                        f"  Step '{s.get('name', '?')}' failed"
                                                        for s in failed_steps
                                                    )
                                                    logs_parts.append(
                                                        f"## Failed: {job.get('name', name)}\n{step_info}"
                                                    )
                                    break

                if not logs_parts or not any(name in p for p in logs_parts):
                    logs_parts.append(f"## Failed: {name} (no detailed logs available)")

            return "\n\n".join(logs_parts) if logs_parts else "CI failed but could not fetch logs"

    except Exception as e:
        log.exception("Failed to fetch CI logs")
        return f"Could not fetch CI logs: {e}"


async def on_ci_failed(event: Event) -> None:
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return
        reason = event.payload.get("reason", "CI checks failed")

        # Fetch actual failure logs if we have a PR URL
        if task.pr_url:
            ci_logs = await _fetch_failed_ci_logs(task.pr_url, settings.github_token)
            reason = f"CI failed. Here are the failure details:\n\n{ci_logs}"

        task = await transition(session, task, TaskStatus.CODING, f"CI failed — fetched logs")
        await session.commit()

        r = await get_redis()
        await publish_event(r, Event(
            type="task.start_coding",
            task_id=task.id,
            payload={"retry_reason": reason[-3000:]},
        ).to_redis())
        await r.aclose()


# Cap on consecutive deploy failures before we stop retrying. Picked low
# because deploy failures rarely fix themselves with another agent loop —
# if two attempts haven't worked, a human needs to look. See task #116.
DEPLOY_RETRY_LIMIT = 2

# Patterns that signal an environmental deploy failure — one that can't be
# fixed by editing code in the repo (billing, missing CLI on the runner,
# expired credentials, quota). We block on the first occurrence so the agent
# doesn't burn cycles "fixing" something it can't reach.
_ENV_FAILURE_PATTERNS: list[tuple[_re.Pattern[str], str]] = [
    (_re.compile(r"\bbilling\b.*\b(disabled|suspended|inactive|required|invalid)\b", _re.I), "billing disabled/required"),
    (_re.compile(r"\b(payment|subscription)\b.*\b(required|suspended|inactive|past[- ]due|invalid|disabled)\b", _re.I), "payment/subscription problem"),
    (_re.compile(r"\b402\s+payment\s+required\b", _re.I), "HTTP 402 payment required"),
    (_re.compile(r"\binsufficient\s+(funds|balance|quota|credit)s?\b", _re.I), "insufficient funds/quota"),
    (_re.compile(r"\bquota\s+(exceeded|exhausted)\b", _re.I), "quota exceeded"),
    (_re.compile(r"\b(aws|gcloud|az|docker|kubectl|terraform)\s*:\s*command not found\b", _re.I), "deploy CLI missing on runner"),
    (_re.compile(r"\bunable to locate credentials\b", _re.I), "missing cloud credentials on runner"),
]


def _classify_deploy_failure(output: str | None) -> str | None:
    """If `output` matches a known environmental-failure pattern, return a short
    reason. Otherwise return None and let the normal retry path run.
    """
    if not output:
        return None
    for pattern, label in _ENV_FAILURE_PATTERNS:
        if pattern.search(output):
            return label
    return None


def _should_block_after_repeated_failures(history) -> bool:
    """Return True if this task has already accumulated `DEPLOY_RETRY_LIMIT`
    'Deploy failed' history entries — i.e. the *next* failure should stop
    retrying instead of re-queueing coding.

    `history` is the task's history rows (only `.message` is read).
    """
    count = 0
    for h in history:
        msg = (getattr(h, "message", None) or "")
        if msg.startswith("Deploy failed"):
            count += 1
            if count >= DEPLOY_RETRY_LIMIT:
                return True
    return False


async def on_dev_deploy_failed(event: Event) -> None:
    """When dev deployment fails, retry coding with the failure output so the
    agent can fix it — unless the failure is environmental (billing, missing
    CLI, etc.) or we've already retried too many times. In those cases, block
    the task and notify the human."""
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return
        output = event.payload.get("output", "")

        env_reason = _classify_deploy_failure(output)

        history_rows = (
            await session.execute(
                sa_select(TaskHistory)
                .where(TaskHistory.task_id == task.id)
                .order_by(TaskHistory.created_at.desc())
            )
        ).scalars().all()
        repeated = _should_block_after_repeated_failures(history_rows)

        if env_reason or repeated:
            block_msg = (
                f"Deploy blocked: {env_reason} — not retryable in repo"
                if env_reason
                else f"Deploy blocked: {DEPLOY_RETRY_LIMIT} consecutive failures, giving up"
            )
            task = await transition(session, task, TaskStatus.BLOCKED, block_msg)
            await session.commit()

            tail = (output or "")[-800:]
            pr_link = f" ({task.pr_url})" if task.pr_url else ""
            await asyncio.to_thread(
                send_telegram,
                f"🚧 Task #{task.id} blocked on dev deploy{pr_link}\n"
                f"Reason: {env_reason or 'repeated deploy failures'}\n"
                f"Last output:\n```\n{tail}\n```",
            )
            return

        # Fetch actual failure logs if we have a PR URL and output is sparse
        if task.pr_url and len(output) < 200:
            ci_logs = await _fetch_failed_ci_logs(task.pr_url, settings.github_token)
            # If GH had no failed check runs to surface (typical for repos
            # without GitHub Actions — the deploy failure happened in the
            # auto-agent's own deploy script), fall back to whatever short
            # output we have. Otherwise the agent retry loop is told
            # "No failed check runs found" and has nothing actionable to fix.
            if _ci_logs_are_empty(ci_logs):
                if output:
                    reason = (
                        "Dev deployment failed (no GitHub Actions check runs to "
                        "fetch). Deploy script output:\n\n"
                        f"{output[-2000:]}"
                    )
                else:
                    reason = (
                        "Dev deployment failed and no diagnostics were captured. "
                        "Check VM logs (~/auto-agent on the deploy host) for "
                        f"docker compose / alembic errors. CI inspector said: {ci_logs}"
                    )
            else:
                reason = f"Deployment failed. Here are the failure details:\n\n{ci_logs}"
        else:
            reason = f"Dev deployment failed. Fix the deployment issue:\n\n{output[-2000:]}"

        task = await transition(session, task, TaskStatus.CODING, f"Deploy failed — fetched logs")
        await session.commit()

        r = await get_redis()
        await publish_event(r, Event(
            type="task.start_coding",
            task_id=task.id,
            payload={"retry_reason": reason[-3000:]},
        ).to_redis())
        await r.aclose()


async def on_review_approved(event: Event) -> None:
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return
        task = await transition(session, task, TaskStatus.DONE, "PR merged, task complete")
        await session.commit()
        await _try_start_queued(session)


# ---------------------------------------------------------------------------
# Continuous-loop scrum master: auto-approve PO suggestions
# ---------------------------------------------------------------------------

# Cap on simultaneously-active freeform tasks per repo, so an enthusiastic PO
# can't flood the queue. When this is hit, new suggestions are left PENDING
# until the queue drains.
MAX_ACTIVE_PER_REPO = 5


async def on_po_suggestions_ready(event: Event) -> None:
    """If the repo has auto_approve_suggestions enabled, convert pending
    suggestions into freeform tasks until the per-repo cap is reached.
    """
    from sqlalchemy import select as sa_select

    repo_name = (event.payload or {}).get("repo_name")
    if not repo_name:
        return

    async with async_session() as session:
        repo_result = await session.execute(sa_select(Repo).where(Repo.name == repo_name))
        repo = repo_result.scalar_one_or_none()
        if not repo:
            return

        config_result = await session.execute(
            sa_select(FreeformConfig).where(FreeformConfig.repo_id == repo.id)
        )
        config = config_result.scalar_one_or_none()
        if not config or not config.enabled or not config.auto_approve_suggestions:
            return

        # Count active freeform tasks for this repo to enforce the cap
        active_result = await session.execute(
            sa_select(Task).where(
                Task.repo_id == repo.id,
                Task.freeform_mode == True,  # noqa: E712
                Task.status.in_([
                    TaskStatus.INTAKE, TaskStatus.CLASSIFYING, TaskStatus.QUEUED,
                    TaskStatus.PLANNING, TaskStatus.AWAITING_APPROVAL,
                    TaskStatus.AWAITING_CLARIFICATION, TaskStatus.CODING,
                    TaskStatus.PR_CREATED, TaskStatus.AWAITING_CI,
                    TaskStatus.AWAITING_REVIEW, TaskStatus.BLOCKED,
                ]),
            )
        )
        active_count = len(active_result.scalars().all())
        slots = MAX_ACTIVE_PER_REPO - active_count
        if slots <= 0:
            log.info(
                f"Auto-approve skipped for '{repo_name}': {active_count} active tasks "
                f"already (cap={MAX_ACTIVE_PER_REPO})"
            )
            return

        # Pull pending suggestions for this repo, highest priority first
        pending_result = await session.execute(
            sa_select(Suggestion)
            .where(
                Suggestion.repo_id == repo.id,
                Suggestion.status == SuggestionStatus.PENDING,
            )
            .order_by(Suggestion.priority.asc(), Suggestion.created_at.asc())
            .limit(slots)
        )
        suggestions = pending_result.scalars().all()
        if not suggestions:
            return

        created_task_ids: list[int] = []
        for suggestion in suggestions:
            task = Task(
                title=suggestion.title,
                description=suggestion.description,
                source=TaskSource.FREEFORM,
                source_id=f"suggestion:{suggestion.id}",
                # Pre-classify as complex so it goes through planning + auto-review
                complexity=TaskComplexity.COMPLEX,
                repo_id=suggestion.repo_id,
                freeform_mode=True,
                intake_qa=intake_qa_for_suggestion(suggestion.category),
            )
            session.add(task)
            await session.flush()
            suggestion.status = SuggestionStatus.APPROVED
            suggestion.task_id = task.id
            created_task_ids.append(task.id)

        await session.commit()
        log.info(
            f"Auto-approved {len(created_task_ids)} suggestions for '{repo_name}' "
            f"(slots remaining: {slots - len(created_task_ids)}): tasks {created_task_ids}"
        )

    r = await get_redis()
    for tid in created_task_ids:
        await publish_event(r, Event(type="task.created", task_id=tid).to_redis())
    await r.aclose()


async def on_task_finished(event: Event) -> None:
    # Clean up workspace and session for the finished task
    if event.task_id:
        r = await get_redis()
        await publish_event(
            r, Event(type="task.cleanup", task_id=event.task_id).to_redis()
        )
        await r.aclose()

    async with async_session() as session:
        await _try_start_queued(session)


async def _try_start_queued(session) -> None:
    for complexity in TaskComplexity:
        if await can_start(session, complexity):
            task = await next_queued_task(session, complexity)
            if not task:
                continue

            # Respect freeform toggle: if the task is freeform but the repo's
            # freeform config is now disabled, skip it — the user turned it off
            # and expects no more freeform tasks to run. The task stays QUEUED
            # and will start if freeform is re-enabled later.
            if task.freeform_mode and task.repo_id:
                from sqlalchemy import select as _sel
                cfg_result = await session.execute(
                    _sel(FreeformConfig).where(FreeformConfig.repo_id == task.repo_id)
                )
                cfg = cfg_result.scalar_one_or_none()
                if not cfg or not cfg.enabled:
                    log.info(
                        f"Skipping freeform task #{task.id}: repo freeform is disabled"
                    )
                    continue

            if complexity == TaskComplexity.COMPLEX:
                task = await transition(session, task, TaskStatus.PLANNING, "Slot opened, starting planning")
            else:
                task = await transition(session, task, TaskStatus.CODING, "Slot opened, starting coding")
            await session.commit()

            r = await get_redis()
            evt = "task.start_planning" if complexity == TaskComplexity.COMPLEX else "task.start_coding"
            await publish_event(r, Event(type=evt, task_id=task.id).to_redis())
            await r.aclose()


async def on_start_queued_task(event: Event) -> None:
    """User requested a queued task to start. Try to start it if a slot is available."""
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task or task.status != TaskStatus.QUEUED:
            return

        if not await can_start(session, task.complexity):
            log.info(f"No slot available for task #{task.id} ({task.complexity.value})")
            return

        if task.complexity in (TaskComplexity.COMPLEX, TaskComplexity.COMPLEX_LARGE):
            task = await transition(session, task, TaskStatus.PLANNING, "Starting planning phase")
        else:
            task = await transition(session, task, TaskStatus.CODING, "Starting coding")
        await session.commit()

        r = await get_redis()
        evt = "task.start_planning" if task.status == TaskStatus.PLANNING else "task.start_coding"
        await publish_event(r, Event(type=evt, task_id=task.id).to_redis())
        await r.aclose()
        log.info(f"Started queued task #{task.id}")


async def _auto_merge_pr(task: Task) -> bool:
    """Merge a PR via GitHub API (squash merge). Returns True on success."""
    from shared.config import settings as _settings
    if not task.pr_url or not _settings.github_token:
        log.warning(f"Cannot auto-merge task #{task.id}: no PR URL or GitHub token")
        return False

    try:
        parts = task.pr_url.rstrip("/").split("/")
        owner, repo, pr_number = parts[-4], parts[-3], parts[-1]
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
                headers={
                    "Authorization": f"token {_settings.github_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                json={"merge_method": "squash"},
            )
            if resp.status_code in (200, 201):
                log.info(f"Auto-merged PR {task.pr_url} for task #{task.id}")
                return True
            log.warning(f"Auto-merge failed for task #{task.id}: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception:
        log.exception(f"Auto-merge error for task #{task.id}")
        return False


# ---------------------------------------------------------------------------
# Event bus wiring
# ---------------------------------------------------------------------------

bus = EventBus()
bus.on("task.created", on_task_created)
bus.on("task.classified", on_task_classified)
bus.on("task.clarification_resolved", on_clarification_resolved)
bus.on("task.approved", on_task_approved)
bus.on("task.review_complete", on_review_complete)
bus.on("task.review_comments_addressed", on_review_comments_addressed)
bus.on("task.ci_passed", on_ci_passed)
bus.on("task.ci_failed", on_ci_failed)
bus.on("task.review_approved", on_review_approved)
bus.on("task.dev_deploy_failed", on_dev_deploy_failed)
bus.on("po.suggestions_ready", on_po_suggestions_ready)
bus.on("task.start_queued", on_start_queued_task)
bus.on("task.done", on_task_finished)
bus.on("task.failed", on_task_finished)


async def orchestrator_event_loop() -> None:
    r = await get_redis()
    await ensure_stream_group(r)
    log.info("Orchestrator event loop started")

    backoff = 1
    max_backoff = 60

    while True:
        try:
            messages = await read_events(r, consumer="orchestrator", count=5, block=5000)
            backoff = 1  # Reset on success
            for msg_id, data in messages:
                try:
                    event = Event.from_redis(data)
                    log.info("processing_event", type=event.type, task_id=event.task_id)
                    await bus.dispatch(event)
                except Exception:
                    log.exception("event_processing_error", msg_id=msg_id)
                finally:
                    await ack_event(r, msg_id, consumer="orchestrator")
        except Exception:
            log.exception("event_loop_error", retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            # Reconnect Redis in case connection dropped
            try:
                r = await get_redis()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Task timeout watchdog — fail tasks stuck too long in active states
# ---------------------------------------------------------------------------

# Timeout thresholds
PLANNING_TIMEOUT = 1200     # 20 minutes for planning
CODING_TIMEOUT_SOFT = 3600  # 1 hour — try recovery first
CODING_TIMEOUT_HARD = 7200  # 2 hours — fail the task
WATCHDOG_INTERVAL = 120     # Check every 2 minutes

TIMED_STATUSES = {TaskStatus.PLANNING, TaskStatus.CODING}

# Track which tasks have already had a recovery attempt (avoid infinite retries)
_recovery_attempted: set[int] = set()


async def _task_has_heartbeat(task_id: int) -> bool:
    """Check if the agent is actively sending heartbeat signals for this task."""
    try:
        r = await get_redis()
        result = await r.exists(f"task:{task_id}:heartbeat")
        await r.aclose()
        return bool(result)
    except Exception:
        return False


async def task_timeout_watchdog() -> None:
    """Progress-aware watchdog. Checks heartbeat before killing tasks.

    Flow for each active task:
    1. If agent heartbeat exists → task is alive, skip.
    2. If no heartbeat AND past soft timeout → attempt recovery (re-emit event).
    3. If no heartbeat AND past hard timeout → fail the task.
    """
    log.info("Task timeout watchdog started")
    while True:
        try:
            async with async_session() as session:
                from sqlalchemy import select as sa_select
                result = await session.execute(
                    sa_select(Task).where(Task.status.in_(TIMED_STATUSES))
                )
                now = datetime.now(timezone.utc)
                for task in result.scalars().all():
                    updated = task.updated_at
                    if updated and updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    if not updated:
                        continue

                    age_s = (now - updated).total_seconds()

                    # Choose timeout based on status
                    soft_timeout = PLANNING_TIMEOUT if task.status == TaskStatus.PLANNING else CODING_TIMEOUT_SOFT
                    hard_timeout = PLANNING_TIMEOUT * 2 if task.status == TaskStatus.PLANNING else CODING_TIMEOUT_HARD

                    # If agent is actively sending heartbeats, it's alive — skip
                    if await _task_has_heartbeat(task.id):
                        if age_s > soft_timeout:
                            log.debug(
                                f"Task #{task.id} past soft timeout ({age_s:.0f}s) "
                                f"but heartbeat is alive — skipping"
                            )
                        continue

                    # No heartbeat — check timeouts
                    if age_s > hard_timeout:
                        # Hard timeout: fail the task
                        log.warning(
                            f"Task #{task.id} hard timeout in {task.status.value} "
                            f"({age_s:.0f}s, no heartbeat)"
                        )
                        task = await transition(
                            session, task, TaskStatus.FAILED,
                            f"Timed out after {age_s:.0f}s in {task.status.value} "
                            f"(no agent heartbeat detected)",
                        )
                        await session.commit()
                        _recovery_attempted.discard(task.id)

                        r = await get_redis()
                        await publish_event(
                            r, Event(type="task.failed", task_id=task.id, payload={
                                "error": f"Hard timeout in {task.status.value}",
                            }).to_redis()
                        )
                        await publish_event(
                            r, Event(type="task.cleanup", task_id=task.id).to_redis()
                        )
                        await r.aclose()

                    elif age_s > soft_timeout and task.id not in _recovery_attempted:
                        # Soft timeout: try recovery once
                        _recovery_attempted.add(task.id)
                        log.warning(
                            f"Task #{task.id} soft timeout in {task.status.value} "
                            f"({age_s:.0f}s, no heartbeat) — attempting recovery"
                        )
                        r = await get_redis()
                        if task.status == TaskStatus.PLANNING:
                            await publish_event(
                                r, Event(type="task.start_planning", task_id=task.id).to_redis()
                            )
                        elif task.status == TaskStatus.CODING:
                            await publish_event(
                                r, Event(type="task.start_coding", task_id=task.id).to_redis()
                            )
                        await r.aclose()

        except Exception:
            log.exception("Watchdog error")
        await asyncio.sleep(WATCHDOG_INTERVAL)


# ---------------------------------------------------------------------------
# CI status poller — fallback when GitHub webhooks aren't configured
# ---------------------------------------------------------------------------

CI_POLL_INTERVAL = 60  # Check every 60 seconds


async def ci_status_poller() -> None:
    """Poll GitHub API for CI status on tasks in AWAITING_CI.

    This is a fallback for when GitHub webhooks aren't configured or can't
    reach the server. Checks the combined commit status and check runs
    for the PR's head branch.
    """
    from shared.config import settings as _settings
    if not _settings.github_token:
        log.info("CI poller: no GITHUB_TOKEN, skipping")
        return

    log.info("CI status poller started")

    while True:
        try:
            async with async_session() as session:
                from sqlalchemy import select as sa_select
                result = await session.execute(
                    sa_select(Task).where(Task.status == TaskStatus.AWAITING_CI)
                )
                tasks_awaiting = result.scalars().all()

                for task in tasks_awaiting:
                    if not task.pr_url:
                        continue

                    try:
                        conclusion = await _check_pr_ci_status(task.pr_url, _settings.github_token)
                    except Exception:
                        log.exception(f"CI poll failed for task #{task.id}")
                        continue

                    if conclusion is None:
                        # CI still running or no checks found — check if repo has no CI at all
                        no_ci = await _pr_has_no_checks(task.pr_url, _settings.github_token)
                        if no_ci:
                            log.info(f"Task #{task.id}: no CI checks on PR, skipping to review")
                            r = await get_redis()
                            await publish_event(r, Event(
                                type="task.ci_passed",
                                task_id=task.id,
                            ).to_redis())
                            await r.aclose()
                        continue

                    r = await get_redis()
                    if conclusion == "success":
                        log.info(f"Task #{task.id}: CI passed (polled)")
                        await publish_event(r, Event(
                            type="task.ci_passed",
                            task_id=task.id,
                        ).to_redis())
                    elif conclusion in ("failure", "timed_out", "action_required"):
                        log.info(f"Task #{task.id}: CI failed: {conclusion} (polled)")
                        await publish_event(r, Event(
                            type="task.ci_failed",
                            task_id=task.id,
                            payload={"reason": f"CI conclusion: {conclusion}"},
                        ).to_redis())
                    await r.aclose()

        except Exception:
            log.exception("CI poller error")
        await asyncio.sleep(CI_POLL_INTERVAL)


async def _check_pr_ci_status(pr_url: str, token: str) -> str | None:
    """Check combined CI status for a PR via GitHub API.

    Returns: 'success', 'failure', 'timed_out', 'action_required', or None if pending/in-progress.
    """
    # Parse owner/repo/number from PR URL
    # e.g. https://github.com/ergodic-ai/cardamon/pull/38
    parts = pr_url.rstrip("/").split("/")
    owner, repo, pr_number = parts[-4], parts[-3], parts[-1]

    import httpx
    async with httpx.AsyncClient() as client:
        # Get PR head SHA
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code != 200:
            return None
        head_sha = resp.json()["head"]["sha"]

        # Check check runs (GitHub Actions)
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code == 200:
            check_runs = resp.json().get("check_runs", [])
            if check_runs:
                # All must complete
                conclusions = [cr.get("conclusion") for cr in check_runs]
                statuses = [cr.get("status") for cr in check_runs]

                if any(s != "completed" for s in statuses):
                    return None  # Still running

                if any(c in ("failure", "timed_out", "action_required") for c in conclusions):
                    return next(c for c in conclusions if c in ("failure", "timed_out", "action_required"))

                if all(c == "success" or c == "skipped" for c in conclusions):
                    return "success"

        # Fallback: check commit status API
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/status",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code == 200:
            state = resp.json().get("state")  # success, failure, pending
            if state == "success":
                return "success"
            elif state == "failure":
                return "failure"
            elif state == "pending":
                return None

    return None


async def _pr_has_no_checks(pr_url: str, token: str) -> bool:
    """Return True if the PR has zero check runs and zero commit statuses."""
    parts = pr_url.rstrip("/").split("/")
    owner, repo, pr_number = parts[-4], parts[-3], parts[-1]

    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code != 200:
            return False
        head_sha = resp.json()["head"]["sha"]

        # Check runs
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code == 200 and resp.json().get("total_count", 0) > 0:
            return False

        # Commit statuses
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/status",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code == 200 and resp.json().get("total_count", 0) > 0:
            return False

    return True


# ---------------------------------------------------------------------------
# PR comment poller — picks up GitHub PR comments without webhooks
# ---------------------------------------------------------------------------

COMMENT_POLL_INTERVAL = 30  # Check every 30 seconds

# ---------------------------------------------------------------------------
# PR merge poller — detects merged PRs without webhooks (exponential backoff)
# ---------------------------------------------------------------------------

MERGE_POLL_INITIAL = 30  # Start polling every 30 seconds
MERGE_POLL_MAX = 600  # Max interval: 10 minutes

# Per-task backoff state: task_id -> current interval
_merge_poll_intervals: dict[int, float] = {}
_merge_poll_last_check: dict[int, float] = {}


async def pr_merge_poller() -> None:
    """Poll GitHub API to detect merged PRs for tasks in AWAITING_REVIEW.

    Uses exponential backoff per task, starting at 30s and maxing out at 10 min.
    """
    from shared.config import settings as _settings
    if not _settings.github_token:
        log.info("PR merge poller: no GITHUB_TOKEN, skipping")
        return

    import time
    log.info("PR merge poller started")

    while True:
        try:
            now = time.monotonic()
            async with async_session() as session:
                from sqlalchemy import select as sa_select
                result = await session.execute(
                    sa_select(Task).where(Task.status == TaskStatus.AWAITING_REVIEW)
                )
                tasks_awaiting = result.scalars().all()

                # Clean up backoff state for tasks no longer awaiting review
                active_ids = {t.id for t in tasks_awaiting}
                for tid in list(_merge_poll_intervals.keys()):
                    if tid not in active_ids:
                        _merge_poll_intervals.pop(tid, None)
                        _merge_poll_last_check.pop(tid, None)

                for task in tasks_awaiting:
                    if not task.pr_url:
                        continue

                    # Initialize backoff for new tasks
                    if task.id not in _merge_poll_intervals:
                        _merge_poll_intervals[task.id] = MERGE_POLL_INITIAL
                        _merge_poll_last_check[task.id] = 0.0

                    # Check if enough time has passed for this task
                    interval = _merge_poll_intervals[task.id]
                    last = _merge_poll_last_check[task.id]
                    if now - last < interval:
                        continue

                    _merge_poll_last_check[task.id] = now

                    try:
                        merged = await _check_pr_merged(task.pr_url, _settings.github_token)
                    except Exception:
                        log.exception(f"Merge poll failed for task #{task.id}")
                        # Back off on errors too
                        _merge_poll_intervals[task.id] = min(interval * 2, MERGE_POLL_MAX)
                        continue

                    if merged:
                        log.info(f"Task #{task.id}: PR merged detected (polled)")
                        r = await get_redis()
                        await publish_event(r, Event(
                            type="task.review_approved",
                            task_id=task.id,
                        ).to_redis())
                        await r.aclose()
                        # Clean up — task will transition out of AWAITING_REVIEW
                        _merge_poll_intervals.pop(task.id, None)
                        _merge_poll_last_check.pop(task.id, None)
                    else:
                        # Exponential backoff
                        _merge_poll_intervals[task.id] = min(interval * 2, MERGE_POLL_MAX)

        except Exception:
            log.exception("PR merge poller error")
        await asyncio.sleep(15)  # Base loop tick — individual tasks have their own intervals


async def _check_pr_merged(pr_url: str, token: str) -> bool:
    """Check if a PR has been merged via GitHub API."""
    parts = pr_url.rstrip("/").split("/")
    owner, repo, pr_number = parts[-4], parts[-3], parts[-1]

    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code != 200:
            return False
        pr_data = resp.json()
        return pr_data.get("merged", False)
REVIEW_POLL_STATUSES = {TaskStatus.AWAITING_REVIEW, TaskStatus.AWAITING_CI, TaskStatus.PR_CREATED}

# Track last seen comment ID per task to avoid re-processing
_last_seen_comments: dict[int, int] = {}


async def pr_comment_poller() -> None:
    """Poll GitHub API for new comments on PRs in review states.

    Picks up both PR review comments (inline) and issue comments (conversation)
    so the agent can respond to feedback even without webhooks configured.
    """
    from shared.config import settings as _settings
    if not _settings.github_token:
        log.info("PR comment poller: no GITHUB_TOKEN, skipping")
        return

    log.info("PR comment poller started")

    while True:
        try:
            async with async_session() as session:
                from sqlalchemy import select as sa_select
                result = await session.execute(
                    sa_select(Task).where(Task.status.in_(REVIEW_POLL_STATUSES))
                )
                for task in result.scalars().all():
                    if not task.pr_url:
                        continue
                    try:
                        await _poll_pr_comments(task, _settings.github_token)
                    except Exception:
                        log.exception(f"Comment poll failed for task #{task.id}")

        except Exception:
            log.exception("PR comment poller error")
        await asyncio.sleep(COMMENT_POLL_INTERVAL)


async def _poll_pr_comments(task: Task, token: str) -> None:
    """Check for new comments on a PR and emit human.message events."""
    parts = task.pr_url.rstrip("/").split("/")
    owner, repo, pr_number = parts[-4], parts[-3], parts[-1]

    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    import httpx
    async with httpx.AsyncClient() as client:
        # Get issue comments (conversation-level comments on the PR)
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
            headers=headers,
            params={"sort": "created", "direction": "desc", "per_page": 10},
        )
        if resp.status_code != 200:
            return

        comments = resp.json()

        # Also get review comments (inline code comments)
        resp2 = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            headers=headers,
            params={"sort": "created", "direction": "desc", "per_page": 10},
        )
        if resp2.status_code == 200:
            comments.extend(resp2.json())

        # Also get PR reviews (formal reviews with body text)
        resp3 = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers=headers,
            params={"per_page": 10},
        )
        if resp3.status_code == 200:
            for review in resp3.json():
                if review.get("body") and review.get("state") in ("CHANGES_REQUESTED", "COMMENTED"):
                    # Treat formal reviews as comments too
                    comments.append({
                        "id": review["id"],
                        "user": review.get("user", {}),
                        "body": f"[Review - {review['state']}] {review['body']}",
                        "created_at": review.get("submitted_at", ""),
                    })

    if not comments:
        return

    # Find the highest comment ID we've already seen
    last_seen = _last_seen_comments.get(task.id, 0)
    new_comments = []
    max_id = last_seen

    for c in comments:
        cid = c.get("id", 0)
        author = c.get("user", {}).get("login", "unknown")
        user_type = c.get("user", {}).get("type", "")
        body = c.get("body", "").strip()

        # Skip bots, empty comments, and already-seen comments
        if user_type == "Bot" or not body or cid <= last_seen:
            continue

        path = c.get("path", "")
        file_context = f" on `{path}`" if path else ""
        new_comments.append((cid, f"[{author}] PR comment{file_context}: {body}"))
        max_id = max(max_id, cid)

    if not new_comments:
        return

    _last_seen_comments[task.id] = max_id

    # Batch all new comments into a single message
    all_feedback = "\n\n---\n\n".join(msg for _, msg in sorted(new_comments))
    log.info(f"Task #{task.id}: {len(new_comments)} new PR comment(s) found (polled)")

    r = await get_redis()
    await publish_event(r, Event(
        type="human.message",
        task_id=task.id,
        payload={
            "message": all_feedback,
            "source": "github_pr_comment_poll",
        },
    ).to_redis())
    await r.aclose()


# ---------------------------------------------------------------------------
# Optional workers (only start if configured)
# ---------------------------------------------------------------------------


async def start_slack_if_configured() -> None:
    from shared.config import settings
    if not settings.slack_bot_token or not settings.slack_app_token:
        log.info("Slack not configured, skipping")
        return
    from integrations.slack.main import main as slack_main
    log.info("Starting Slack worker")
    await slack_main()


# ---------------------------------------------------------------------------
# Startup recovery — re-emit events for tasks stuck in active states
# ---------------------------------------------------------------------------


async def _recover_stuck_tasks() -> None:
    """On startup, find tasks stuck in active states and re-emit their events.

    This handles the case where the container restarted mid-task and the
    original event was already acknowledged.

    Covers:
    - PLANNING → re-emit start_planning
    - CODING → re-emit start_coding
    - AWAITING_APPROVAL (freeform + has plan) → re-emit plan_ready so
      the independent plan reviewer triggers. Without this, freeform
      scaffold tasks get stuck after a restart (see task 52 incident).
    """
    from sqlalchemy import select as sa_select
    async with async_session() as session:
        result = await session.execute(
            sa_select(Task).where(
                Task.status.in_({
                    TaskStatus.PLANNING,
                    TaskStatus.CODING,
                    TaskStatus.AWAITING_APPROVAL,
                })
            )
        )
        stuck_tasks = result.scalars().all()

        if not stuck_tasks:
            return

        r = await get_redis()
        for task in stuck_tasks:
            if task.status == TaskStatus.PLANNING:
                log.info(f"Recovering task #{task.id}: re-emitting start_planning")
                await publish_event(r, Event(type="task.start_planning", task_id=task.id).to_redis())
            elif task.status == TaskStatus.CODING:
                if task.complexity == TaskComplexity.SIMPLE_NO_CODE:
                    log.info(f"Recovering query task #{task.id}: re-emitting task.query")
                    await publish_event(r, Event(type="task.query", task_id=task.id).to_redis())
                else:
                    log.info(f"Recovering task #{task.id}: re-emitting start_coding")
                    await publish_event(r, Event(type="task.start_coding", task_id=task.id).to_redis())
            elif task.status == TaskStatus.AWAITING_APPROVAL and task.freeform_mode:
                log.info(f"Recovering freeform task #{task.id}: re-emitting plan_ready for auto-review")
                await publish_event(
                    r, Event(type="task.plan_ready", task_id=task.id, payload={"plan": task.plan or ""}).to_redis()
                )
        await r.aclose()
        log.info(f"Recovered {len(stuck_tasks)} stuck task(s)")

    # Also try starting any queued tasks that may have been left behind
    async with async_session() as session:
        await _try_start_queued(session)


# ---------------------------------------------------------------------------
# Lifespan — one place to start everything
# ---------------------------------------------------------------------------


def _run_alembic_upgrade_sync() -> None:
    """Apply pending alembic migrations. Runs synchronously in its own thread.

    The deploy script (scripts/deploy.sh) only runs migrations when invoked
    with the explicit `migrate` flag, so a default deploy ships new code
    against an old schema. New columns added to existing tables would
    silently break SQL queries (Base.metadata.create_all only creates
    missing TABLES, not missing COLUMNS). Running alembic at startup keeps
    the schema in sync with the code that's about to run, regardless of
    how the container was deployed.

    Must run in a thread (not the lifespan event loop) because
    migrations/env.py::run_migrations_online calls asyncio.run(), which
    can't be invoked from within a running loop.

    Idempotent — alembic skips revisions already applied.
    """
    from pathlib import Path as _Path

    from alembic import command
    from alembic.config import Config

    cfg_path = _Path(__file__).parent / "alembic.ini"
    if not cfg_path.is_file():
        log.warning("alembic.ini not found, skipping startup migration")
        return
    cfg = Config(str(cfg_path))
    try:
        command.upgrade(cfg, "head")
    except Exception:
        # Don't crash startup on a migration error — log and let the app
        # boot. A broken migration is the operator's problem to fix; the
        # process should still come up so the existing app keeps serving.
        log.exception("alembic upgrade head failed at startup")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Run alembic migrations BEFORE create_all so we never query columns
    # that don't exist yet on the deployed DB. Threaded to dodge the
    # nested-event-loop problem in migrations/env.py.
    await asyncio.to_thread(_run_alembic_upgrade_sync)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed admin user if no users exist
    from orchestrator.router import seed_admin_user
    await seed_admin_user()

    # Auto-discover repos from GitHub
    async with async_session() as session:
        await sync_repos(session)

    # Recover tasks stuck in active states (e.g. from a restart mid-task)
    await _recover_stuck_tasks()

    bg = [
        asyncio.create_task(orchestrator_event_loop()),
        asyncio.create_task(run_scheduler()),
        asyncio.create_task(claude_runner_loop()),
        asyncio.create_task(telegram_inbound_loop()),
        asyncio.create_task(telegram_notification_loop()),
        asyncio.create_task(web_event_listener()),
        asyncio.create_task(start_slack_if_configured()),
        asyncio.create_task(task_timeout_watchdog()),
        asyncio.create_task(ci_status_poller()),
        asyncio.create_task(pr_comment_poller()),
        asyncio.create_task(pr_merge_poller()),
        asyncio.create_task(run_po_analysis_loop()),
        asyncio.create_task(run_architecture_loop()),
    ]

    send_telegram("Auto-agent is online and ready for tasks.")
    log.info("All systems started — http://localhost:2020")

    yield

    send_telegram("Auto-agent is shutting down.")
    for t in bg:
        t.cancel()
    for t in bg:
        with contextlib.suppress(asyncio.CancelledError):
            await t


app.router.lifespan_context = lifespan


if __name__ == "__main__":
    from shared.preflight import check_all
    print("Running preflight checks...")
    check_all()
    uvicorn.run("run:app", host="0.0.0.0", port=2020, reload=False)
