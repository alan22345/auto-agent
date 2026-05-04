"""Query phase — answer a SIMPLE_NO_CODE task with a single LLM call.

No repo, no git, no tools — just send the task description to the LLM and
return the response. The answer is saved to the task's ``plan`` field (the
50K-limit text column) and a truncated preview goes into the transition
message.
"""

from __future__ import annotations

import httpx

from agent.lifecycle._orchestrator_api import (
    ORCHESTRATOR_URL,
    get_task,
    transition_task,
)
from agent.llm import get_provider
from agent.llm.types import Message
from shared.events import Event
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.query")


async def handle_query(task_id: int) -> None:
    """Handle a SIMPLE_NO_CODE task — just answer the question via a single LLM call."""
    task = await get_task(task_id)
    if not task:
        return

    log.info(f"Handling query task #{task_id}: {task.title[:100]}")

    try:
        provider = get_provider(model_override="standard")

        response = await provider.complete(
            messages=[
                Message(
                    role="user",
                    content=(
                        f"{task.title}\n\n{task.description or ''}\n\n"
                        "Answer this question thoroughly and concisely. "
                        "If you need to browse a URL, say so — but give the best answer you can from your knowledge."
                    ),
                ),
            ],
            max_tokens=4096,
        )
        answer = response.message.content

        # Close the async client before transitioning
        if hasattr(provider, "_client"):
            try:
                if hasattr(provider._client, "_client") and hasattr(
                    provider._client._client, "aclose"
                ):
                    await provider._client._client.aclose()
                elif hasattr(provider._client, "close"):
                    provider._client.close()
            except Exception:
                pass

        # Save answer: plan field holds the full response (50K limit), message
        # field gets a truncated preview (2K limit on TransitionRequest.message).
        msg_preview = answer[:1900] + "..." if len(answer) > 1900 else answer
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/transition",
                json={
                    "status": "done",
                    "message": f"Answer:\n\n{msg_preview}",
                    "plan": answer,
                },
            )
            if resp.status_code >= 400:
                log.error(
                    f"Query task #{task_id}: transition to done failed "
                    f"({resp.status_code}): {resp.text[:200]}"
                )
        log.info(f"Query task #{task_id} completed ({len(answer)} chars)")

    except Exception as e:
        log.exception(f"Query task #{task_id} failed")
        await transition_task(task_id, "failed", str(e))


async def handle(event: Event) -> None:
    """EventBus entry — answers a query task."""
    if not event.task_id:
        return
    await handle_query(event.task_id)
