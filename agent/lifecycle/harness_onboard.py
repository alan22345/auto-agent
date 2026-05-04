"""Harness onboarding shim — wires the EventBus to ``agent.harness``.

The actual onboarding flow lives in ``agent.harness.handle_harness_onboarding``
(it's substantial enough to warrant its own module). This file just adapts
that call site to the EventBus's ``handle(event)`` shape.
"""

from __future__ import annotations

from agent.harness import handle_harness_onboarding
from shared.events import Event
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.harness_onboard")


async def handle(event: Event) -> None:
    """EventBus entry — runs harness onboarding for a repo."""
    if not event.payload:
        return
    repo_id = event.payload.get("repo_id")
    repo_name = event.payload.get("repo_name", "")
    if not repo_id:
        return
    await handle_harness_onboarding(repo_id, repo_name)
