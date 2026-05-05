"""Conflict resolution phase — resolve merge conflicts on a freeform PR.

Triggered by `task.merge_conflict_detected`. Hands off to
`agent.conflict_resolver.handle_merge_conflict_resolution`, which clones the
feature branch, merges the base branch in, runs the agent loop on any
conflicts, and pushes the resulting merge commit. Outcome is announced via
`task.merge_conflict_resolved` or `task.merge_conflict_resolution_failed`.
"""

from __future__ import annotations

from agent.conflict_resolver import handle_merge_conflict_resolution
from shared.events import Event
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.conflict_resolve")


async def handle(event: Event) -> None:
    if not event.task_id:
        return
    pr_url = (event.payload or {}).get("pr_url", "")
    if not pr_url:
        log.warning(f"task.merge_conflict_detected on task #{event.task_id} missing pr_url")
        return
    await handle_merge_conflict_resolution(event.task_id, pr_url)
