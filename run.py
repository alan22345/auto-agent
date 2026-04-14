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
import base64
import contextlib
import secrets
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
    TaskSource,
    TaskStatus,
)
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
from claude_runner.main import event_loop as claude_runner_loop
from claude_runner.po_analyzer import run_po_analysis_loop

log = setup_logging("auto-agent")


# ---------------------------------------------------------------------------
# Unified FastAPI app — API + webhooks + web UI, all on one port
# ---------------------------------------------------------------------------

app = FastAPI(title="Auto-Agent", version="0.1.0")


# ---------------------------------------------------------------------------
# HTTP Basic auth middleware
#
# Protects the web UI and API from random visitors. Stays out of the way of:
#   - /health (so the deploy script's health check still works)
#   - /api/webhooks/* (GitHub/Linear webhooks need to be reachable)
#   - Loopback requests from inside the same container (web -> orchestrator)
#
# Set WEB_AUTH_PASSWORD in .env to enable. Empty = disabled.
# ---------------------------------------------------------------------------

_AUTH_EXEMPT_PREFIXES = ("/health", "/api/webhooks/")


def _is_auth_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES)


@app.middleware("http")
async def basic_auth_middleware(request, call_next):
    password = settings.web_auth_password
    if not password:
        return await call_next(request)

    if _is_auth_exempt(request.url.path):
        return await call_next(request)

    # Allow internal HTTP calls between services running in the same container.
    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "::1"):
        return await call_next(request)

    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="ignore")
            user, _, supplied = decoded.partition(":")
            if user == "admin" and secrets.compare_digest(supplied, password):
                return await call_next(request)
        except Exception:
            pass

    return Response(
        status_code=401,
        content="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="auto-agent"'},
    )


# API
app.include_router(api_router, prefix="/api")
app.include_router(metrics_router, prefix="/api")

# Webhooks
app.include_router(github_webhook_router, prefix="/api")
app.include_router(linear_webhook_router, prefix="/api")

# Static files for web UI
STATIC_DIR = Path(__file__).parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text())


def _websocket_authorized(ws: WebSocket) -> bool:
    password = settings.web_auth_password
    if not password:
        return True
    client_host = ws.client.host if ws.client else ""
    if client_host in ("127.0.0.1", "::1"):
        return True
    auth = ws.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="ignore")
        user, _, supplied = decoded.partition(":")
        return user == "admin" and secrets.compare_digest(supplied, password)
    except Exception:
        return False


@app.websocket("/ws")
async def ws_proxy(ws: WebSocket) -> None:
    if not _websocket_authorized(ws):
        await ws.close(code=1008)
        return
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

        # Auto-match repo from task text if not already set
        if not task.repo_id:
            repo = await match_repo(session, f"{task.title} {task.description}")
            if repo:
                task.repo_id = repo.id
                log.info(f"Auto-matched repo '{repo.name}' for task #{task.id}")

        task = await transition(session, task, TaskStatus.CLASSIFYING, "Auto-classifying")
        await session.commit()

        # If complexity was pre-set at task creation (e.g. scaffold tasks from
        # the create-repo flow), trust it and skip the keyword classifier.
        if task.complexity is None:
            complexity, classification = classify_task(task.title, task.description)
            task.complexity = complexity
            await session.commit()
            payload = {"complexity": complexity.value, **classification.model_dump()}
        else:
            log.info(f"Task #{task.id} pre-classified as {task.complexity.value}, skipping classifier")
            payload = {"complexity": task.complexity.value, "reasoning": "Pre-classified at creation"}

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

        # Freeform tasks with auto_start_tasks bypass the concurrency queue
        force_start = False
        if task.freeform_mode and task.repo_id:
            from sqlalchemy import select as _sel
            cfg_result = await session.execute(
                _sel(FreeformConfig).where(FreeformConfig.repo_id == task.repo_id)
            )
            cfg = cfg_result.scalar_one_or_none()
            if cfg and cfg.auto_start_tasks:
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


async def on_ci_passed(event: Event) -> None:
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return

        if task.freeform_mode:
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

        task = await transition(session, task, TaskStatus.AWAITING_REVIEW, "CI passed, awaiting human review")
        await session.commit()

    # Trigger dev deploy so the user can review a live preview
    r = await get_redis()
    await publish_event(
        r, Event(type="task.deploy_preview", task_id=event.task_id).to_redis()
    )
    await r.aclose()


async def on_ci_failed(event: Event) -> None:
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return
        reason = event.payload.get("reason", "CI checks failed")
        task = await transition(session, task, TaskStatus.CODING, f"CI failed: {reason}")
        await session.commit()

        r = await get_redis()
        await publish_event(r, Event(
            type="task.start_coding",
            task_id=task.id,
            payload={"retry_reason": reason},
        ).to_redis())
        await r.aclose()


async def on_dev_deploy_failed(event: Event) -> None:
    """When dev deployment fails, retry coding with the failure output so Claude can fix it."""
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task:
            return
        output = event.payload.get("output", "Deployment failed (no details)")
        # Truncate to avoid oversized prompts
        reason = f"Dev deployment failed. Fix the deployment issue:\n\n{output[-2000:]}"
        task = await transition(session, task, TaskStatus.CODING, f"Deploy failed: {output[:200]}")
        await session.commit()

        r = await get_redis()
        await publish_event(r, Event(
            type="task.start_coding",
            task_id=task.id,
            payload={"retry_reason": reason},
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
            if task:
                if complexity == TaskComplexity.COMPLEX:
                    task = await transition(session, task, TaskStatus.PLANNING, "Slot opened, starting planning")
                else:
                    task = await transition(session, task, TaskStatus.CODING, "Slot opened, starting coding")
                await session.commit()

                r = await get_redis()
                evt = "task.start_planning" if complexity == TaskComplexity.COMPLEX else "task.start_coding"
                await publish_event(r, Event(type=evt, task_id=task.id).to_redis())
                await r.aclose()


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

TASK_TIMEOUT_SECONDS = 3600  # 1 hour
WATCHDOG_INTERVAL = 120  # Check every 2 minutes

TIMED_STATUSES = {TaskStatus.PLANNING, TaskStatus.CODING}


async def task_timeout_watchdog() -> None:
    """Periodically check for tasks stuck in active states and fail them."""
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
                    if updated and (now - updated).total_seconds() > TASK_TIMEOUT_SECONDS:
                        log.warning(
                            f"Task #{task.id} timed out in {task.status.value} "
                            f"(stuck for {(now - updated).total_seconds():.0f}s)"
                        )
                        task = await transition(
                            session, task, TaskStatus.FAILED,
                            f"Timed out after {TASK_TIMEOUT_SECONDS}s in {task.status.value}",
                        )
                        await session.commit()

                        r = await get_redis()
                        await publish_event(
                            r, Event(type="task.failed", task_id=task.id, payload={
                                "error": f"Timed out in {task.status.value}",
                            }).to_redis()
                        )
                        await publish_event(
                            r, Event(type="task.cleanup", task_id=task.id).to_redis()
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
    """On startup, find tasks stuck in PLANNING or CODING and re-emit their events.

    This handles the case where the container restarted mid-task and the
    original event was already acknowledged.
    """
    from sqlalchemy import select as sa_select
    async with async_session() as session:
        result = await session.execute(
            sa_select(Task).where(Task.status.in_({TaskStatus.PLANNING, TaskStatus.CODING}))
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
                log.info(f"Recovering task #{task.id}: re-emitting start_coding")
                await publish_event(r, Event(type="task.start_coding", task_id=task.id).to_redis())
        await r.aclose()
        log.info(f"Recovered {len(stuck_tasks)} stuck task(s)")


# ---------------------------------------------------------------------------
# Lifespan — one place to start everything
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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
