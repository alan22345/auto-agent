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

from typing import TYPE_CHECKING, Any

import structlog
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
from shared.models import FreeformConfig, Repo, Task, TaskStatus

if TYPE_CHECKING:
    from shared.types import FreeformConfigData, RepoData, TaskData

ORCHESTRATOR_URL = settings.orchestrator_url

log = structlog.get_logger()


async def get_task(task_id: int) -> TaskData | None:
    """Fetch a task as ``TaskData`` directly from the DB (in-process).

    This used to call ``GET /tasks/{id}`` over the HTTP loopback, but that
    endpoint requires ``current_org_id_dep`` (org-scoped auth) and the agent
    has no session cookie or bearer token — so the call 401'd and returned
    ``None``, silently dropping every inbound message in
    ``route_human_message`` (clarification replies, PR-review routing, …).
    Reading in-process via the DB — the same fix ``transition_task`` already
    applies — sidesteps auth entirely. Reuses the canonical ORM→TaskData
    mapping so the wire shape (``repo_name``/``effective_mode``/…) is identical
    to the HTTP endpoint.
    """
    from sqlalchemy.orm import selectinload

    from orchestrator.router import _task_to_response

    async with async_session() as s:
        task = (
            await s.execute(
                select(Task).options(selectinload(Task.repo)).where(Task.id == task_id)
            )
        ).scalar_one_or_none()
        if task is None:
            return None
        return _task_to_response(task)


async def get_repo(repo_name: str) -> RepoData | None:
    """Fetch a repo as ``RepoData`` directly from the DB (in-process).

    Like ``get_task``, this used to call the HTTP loopback (``GET /repos``),
    but that endpoint requires ``current_org_id_dep`` (org-scoped auth) and the
    agent has no session cookie or bearer token — so the call 401'd and we
    blocked every coding task with a misleading "repo not found". Reading
    in-process via the DB sidesteps auth entirely and reuses the canonical
    ORM→RepoData mapping used by ``GET /repos``.
    """
    from orchestrator.router import _repo_to_data

    async with async_session() as s:
        repo = (
            await s.execute(select(Repo).where(Repo.name == repo_name))
        ).scalar_one_or_none()
        if repo is None:
            return None
        return _repo_to_data(repo)


async def mark_repo_harness_onboarded(repo_id: int, pr_url: str | None) -> None:
    """Mark a repo harness-onboarded in-process (DB write).

    Same auth trap as ``get_repo``: the POST /repos/{id}/harness loopback is
    org-scoped, so the unauthenticated agent call 401'd and the repo never got
    marked onboarded — onboarding re-fired on every coding task, opening
    duplicate harness PRs. Write in-process instead.
    """
    async with async_session() as s:
        repo = (
            await s.execute(select(Repo).where(Repo.id == repo_id))
        ).scalar_one_or_none()
        if repo is None:
            log.error("mark_repo_harness_onboarded: repo not found", repo_id=repo_id)
            return
        repo.harness_onboarded = True
        repo.harness_pr_url = pr_url
        await s.commit()


async def get_freeform_config(repo_name: str) -> FreeformConfigData | None:
    """Fetch a repo's enabled freeform config from the DB (in-process).

    Same auth trap as ``get_repo``: the HTTP loopback (``GET /freeform/config``)
    is org-scoped, so the unauthenticated agent call 401'd and silently dropped
    dev-branch targeting for every freeform task. Read in-process instead.
    """
    from orchestrator.router import _freeform_config_to_response

    async with async_session() as s:
        repo = (
            await s.execute(select(Repo).where(Repo.name == repo_name))
        ).scalar_one_or_none()
        if repo is None:
            return None
        cfg = (
            await s.execute(
                select(FreeformConfig).where(FreeformConfig.repo_id == repo.id)
            )
        ).scalar_one_or_none()
        if cfg is None or not cfg.enabled:
            return None
        return _freeform_config_to_response(cfg, repo.name)


async def set_task_branch(task_id: int, branch_name: str) -> None:
    """Persist a task's branch name in-process (DB write).

    Same auth trap as ``get_repo``: the PATCH /tasks/{id}/branch loopback is
    org-scoped, so the unauthenticated agent call 401'd — and the caller never
    checked the response status, so the write was silently dropped. The branch
    was created locally but never recorded, so the verify push aborted with
    "task.branch_name missing — cannot push" and the task failed *after* a
    clean, intent-passing implementation (task #327). Write in-process instead.
    """
    async with async_session() as s:
        task = (
            await s.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            log.error("set_task_branch: task not found", task_id=task_id)
            return
        task.branch_name = branch_name
        await s.commit()


async def set_task_affected_routes(task_id: int, routes: list[dict]) -> None:
    """Persist the planner-declared affected routes for a task (in-process).

    Same auth trap as ``get_repo``: the POST /tasks/{id}/affected_routes
    loopback is org-scoped, so the unauthenticated agent call 401'd and the
    routes silently never persisted — verify/UI route exercising then had
    nothing to check. Write in-process instead.
    """
    async with async_session() as s:
        task = (
            await s.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            log.error("set_task_affected_routes: task not found", task_id=task_id)
            return
        task.affected_routes = routes
        await s.commit()


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
