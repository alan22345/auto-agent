"""HTTP calls into the orchestrator API.

Every lifecycle module that needs to fetch task/repo state or transition a
task uses these helpers. The orchestrator URL comes from ``shared.config``.

``transition_task`` is the exception: it runs in-process via the state
machine instead of going through the HTTP loopback. The HTTP endpoint
requires ``current_org_id_dep`` (org-scoped auth) and the agent has no
session cookie or bearer token, so the loopback used to 401 silently —
the event still fired (Slack/UI announced "awaiting approval") but the
DB transition never happened, leaving tasks stuck at TRIO_EXECUTING.
The boundary that agent/ must not import from orchestrator/ is already
relaxed for ``agent/lifecycle/trio`` which imports the state machine
directly; this file follows the same pattern.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select

from shared.config import settings
from shared.database import async_session
from shared.events import (
    publish,
    task_awaiting_design_approval,
    task_awaiting_plan_approval,
    task_blocked,
    task_done,
    task_failed,
)
from shared.models import Task, TaskStatus
from shared.types import FreeformConfigData, RepoData, TaskData

ORCHESTRATOR_URL = settings.orchestrator_url


async def get_task(task_id: int) -> TaskData | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}")
        if resp.status_code == 200:
            return TaskData.model_validate(resp.json())
    return None


async def get_repo(repo_name: str) -> RepoData | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/repos")
        repos = resp.json()
        for repo_dict in repos:
            repo = RepoData.model_validate(repo_dict)
            if repo.name == repo_name:
                return repo
    return None


async def get_freeform_config(repo_name: str) -> FreeformConfigData | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/freeform/config")
        if resp.status_code != 200:
            return None
        configs = resp.json()
        for cfg in configs:
            cfg_data = FreeformConfigData.model_validate(cfg)
            if cfg_data.repo_name == repo_name and cfg_data.enabled:
                return cfg_data
    return None


async def set_task_affected_routes(task_id: int, routes: list[dict]) -> None:
    """Persist the planner-declared affected routes for a task."""
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/affected_routes",
            json={"routes": routes},
            timeout=10.0,
        )


# Wire-status → event factory. Covers both terminal states (failed,
# blocked, done) and notification states that need a Slack/Telegram
# nudge so a user knows a gate is open. ADR-015 Phase 7.7 added the two
# awaiting-approval entries.
_NOTIFY_FACTORIES = {
    "failed": lambda task_id, message, _extras: task_failed(task_id, error=message),
    "blocked": lambda task_id, message, _extras: task_blocked(task_id, error=message),
    "done": lambda task_id, _message, _extras: task_done(task_id),
    "awaiting_design_approval": lambda task_id, message, extras: (
        task_awaiting_design_approval(task_id, message=message, **extras)
    ),
    "awaiting_plan_approval": lambda task_id, message, extras: (
        task_awaiting_plan_approval(task_id, message=message, **extras)
    ),
}


async def transition_task(
    task_id: int, status: str, message: str = "", **extras: Any,
) -> None:
    """Transition a task and publish the matching wire event.

    In-process: opens its own ``async_session``, runs the state machine,
    commits, then publishes. The state machine raises ``InvalidTransition``
    when the requested status isn't reachable from the current one — we
    let it propagate so callers see the failure instead of writing it
    only into a log line. Publish only fires after a successful commit,
    so a transition failure no longer falsely surfaces as a notification.

    ``**extras`` are forwarded as keyword arguments to the matched event
    factory — e.g. ``design_md=...`` for ``awaiting_design_approval`` —
    so callers can ship rich payload through the same transition seam
    without bypassing the state-machine gate.
    """
    from orchestrator.state_machine import transition

    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        await transition(s, task, TaskStatus(status), message)
        await s.commit()

    factory = _NOTIFY_FACTORIES.get(status)
    if factory is not None:
        await publish(factory(task_id, message, extras))
