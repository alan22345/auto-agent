"""Linear webhook handler — receives issue events from Linear.

Configure in Linear settings → Webhooks:
  URL: https://<your-domain>/api/webhooks/linear
  Events: Issues
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from shared.config import settings
from shared.events import Event
from shared.redis_client import get_redis, publish_event
from shared.types import LinearIssue

log = logging.getLogger(__name__)

router = APIRouter()

ORCHESTRATOR_URL = settings.orchestrator_url


@router.post("/webhooks/linear")
async def linear_webhook(request: Request) -> dict[str, str]:
    """Handle incoming Linear webhook events.

    Linear sends:
      action: "create" | "update" | "remove"
      type: "Issue" | "Comment" | ...
      data: { id, identifier, title, description, state { name }, ... }
    """
    payload: dict[str, Any] = await request.json()

    action: str = payload.get("action", "")
    resource_type: str = payload.get("type", "")

    if resource_type != "Issue":
        return {"status": "ignored"}

    if action in ("create", "update"):
        data = payload.get("data", {})
        state_name: str = data.get("state", {}).get("name", "").lower()

        if state_name in ("backlog", "todo", "in progress", "unstarted", "started"):
            try:
                issue = LinearIssue.model_validate(data)
                await _sync_issue(issue)
            except ValidationError:
                log.warning(f"Skipping invalid Linear issue: {data.get('id', '?')}")

    return {"status": "ok"}


async def _sync_issue(issue: LinearIssue) -> None:
    """Emit a task.created event for a Linear issue.

    The orchestrator's /tasks endpoint handles dedup, so we go through it.
    """
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/tasks",
            json={
                "title": f"[{issue.identifier}] {issue.title}",
                "description": issue.description,
                "source": "linear",
                "source_id": issue.id,
            },
        )
        if resp.status_code == 200:
            task_id = resp.json().get("id")
            log.info(f"Synced Linear issue {issue.identifier} → Task #{task_id}")
        else:
            log.error(f"Failed to sync {issue.identifier}: {resp.status_code}")
