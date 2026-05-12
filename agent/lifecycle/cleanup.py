"""Cleanup phase — remove a finished task's workspace.

Tiny module by design — but it earns its place in the lifecycle hierarchy:
"clean up after a task" is a distinct phase that's worth naming.
"""

from __future__ import annotations

from agent.lifecycle._orchestrator_api import get_task
from agent.workspace import cleanup_workspace
from shared.events import Event
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.cleanup")


async def handle_task_cleanup(task_id: int) -> None:
    """Clean up workspace and session for a finished task."""
    log.info(f"Cleaning up workspace for task #{task_id}")
    task = await get_task(task_id)
    org_id = task.organization_id if task else None
    cleanup_workspace(task_id, organization_id=org_id)


async def handle(event: Event) -> None:
    """EventBus entry — cleans up a finished task's workspace."""
    if not event.task_id:
        return
    await handle_task_cleanup(event.task_id)
