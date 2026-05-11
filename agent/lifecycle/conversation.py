"""Conversation phase — human-driven resume flows + human.message routing.

Three handlers:
  - ``handle_plan_conversation`` — user discusses the plan with the planning
    agent; agent may revise the plan or just chat.
  - ``handle_clarification_response`` — user answered a CLARIFICATION_NEEDED
    question; resume the agent. Special case: grill-phase clarifications
    fill in the trailing pending intake_qa entry and re-enter planning.
  - ``handle_blocked_response`` — user provided input on a blocked task;
    routes to coding/planning/PR-review depending on what's available.

``route_human_message`` is the EventBus entry for ``human.message`` — it
maps task status to one of the three handlers above (or pushes the message
as guidance into the active agent's Redis queue when no handler change is
needed).
"""

from __future__ import annotations

import os
import re as _re

import httpx

from agent.lifecycle import coding, planning, review
from agent.lifecycle._clarification import _extract_clarification
from agent.lifecycle._naming import _session_id
from agent.lifecycle._orchestrator_api import (
    ORCHESTRATOR_URL,
    get_repo,
    get_task,
    transition_task,
)
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.workspace import WORKSPACES_DIR
from shared.events import (
    Event,
    publish,
    task_blocked,
    task_clarification_needed,
    task_clarification_resolved,
    task_plan_ready,
    task_start_queued,
    task_status_changed,
)
from shared.logging import setup_logging
from shared.task_channel import task_channel
from shared.types import TaskData

log = setup_logging("agent.lifecycle.conversation")


# Track which tasks have an active conversation running. Module-level sets
# guard against re-entrant in-process invocations (a second message arriving
# while the first is still running) — duplicate messages are pushed as
# guidance instead of spawning a parallel agent.
_active_plan_conversations: set[int] = set()
_active_clarification_tasks: set[int] = set()


async def handle_plan_conversation(task_id: int, message: str) -> None:
    """Resume the planning session so the user can discuss the plan with the agent.

    The agent retains full context from planning — the user's message is injected
    as a continuation and the agent responds. If the agent produces a revised plan
    (detected by a markdown heading), the plan is updated. Otherwise the response
    is streamed as a chat message.
    """
    if task_id in _active_plan_conversations:
        log.info(f"Task #{task_id} plan conversation already active — pushing as guidance")
        await task_channel(task_id).push_guidance(message)
        return

    task = await get_task(task_id)
    if not task or not task.repo_name:
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    session_id = _session_id(task_id, task.created_at)
    workspace = os.path.join(WORKSPACES_DIR, f"task-{task_id}")

    log.info(f"Plan conversation for task #{task_id}: {message[:100]}...")

    _active_plan_conversations.add(task_id)
    try:
        agent = create_agent(
            workspace,
            session_id=session_id,
            max_turns=10,
            task_id=task_id,
            readonly=True,
            task_description=task.description,
            repo_name=repo.name,
            home_dir=await home_dir_for_task(task),
        )
        result = await agent.run(message, resume=True)
        output = result.output

        # If the agent revised the plan (contains markdown headings like ## Task),
        # update the stored plan
        if output and _re.search(r"^#{1,3} ", output, _re.MULTILINE):
            full_output = (
                "\n".join(
                    msg.content
                    for msg in result.messages
                    if msg.role == "assistant" and msg.content
                )
                or output
            )
            full_output = planning._trim_plan_text(full_output)

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{ORCHESTRATOR_URL}/tasks/{task_id}/transition",
                    json={
                        "status": "awaiting_approval",
                        "message": "Plan revised",
                        "plan": full_output,
                    },
                )
                resp.raise_for_status()
            await publish(task_plan_ready(task_id, plan=full_output))
        else:
            await publish(
                task_status_changed(
                    task_id, status=task.status, message=output[:2000]
                )
            )

        log.info(f"Plan conversation response for task #{task_id}: {output[:200]}...")
    finally:
        _active_plan_conversations.discard(task_id)


async def handle_clarification_response(task_id: int, answer: str) -> None:
    """Resume a task after the user answered a clarification question."""
    # Guard against concurrent resumes — if the agent is already running from
    # a previous clarification answer, push this message as guidance instead
    if task_id in _active_clarification_tasks:
        log.info(f"Task #{task_id} already resuming — pushing as guidance")
        await task_channel(task_id).push_guidance(answer)
        return

    task = await get_task(task_id)
    if not task or not task.repo_name:
        return

    # Grill-phase clarification: fill in the trailing {question, answer: None}
    # entry on intake_qa, transition back to PLANNING, and re-trigger the
    # planning handler. The next turn either asks the next question or emits
    # GRILL_DONE.
    if task.intake_qa and task.plan is None:
        last = task.intake_qa[-1] if task.intake_qa else None
        if isinstance(last, dict) and last.get("answer") is None:
            log.info(f"Task #{task_id} GRILL answer received — re-entering grill loop")
            updated_qa = list(task.intake_qa)
            updated_qa[-1] = {**last, "answer": answer}
            await planning._save_intake_qa(task_id, updated_qa)
            await transition_task(task_id, "planning", "User answered grill question")
            await planning.handle_planning(task_id)
            return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    session_id = _session_id(task_id, task.created_at)
    workspace = os.path.join(WORKSPACES_DIR, f"task-{task_id}")

    log.info(f"Resuming task #{task_id} with clarification answer (session={session_id})")

    _active_clarification_tasks.add(task_id)
    try:
        agent = create_agent(
            workspace,
            session_id=session_id,
            max_turns=40,
            task_id=task_id,
            task_description=task.description,
            repo_name=repo.name,
            home_dir=await home_dir_for_task(task),
        )
        result = await agent.run(
            (
                f"The user replied:\n\n{answer}\n\n"
                "If they answered your question, continue with the task. "
                "If they asked something back, pushed back, or deferred to you "
                '("I don\'t know, what do you suggest?"), address that in 1-2 '
                "sentences on the lines AFTER a new `CLARIFICATION_NEEDED:` "
                "(those lines are shown to the user) and re-ask your question "
                "(or rephrase it). Treat this as a real conversation, not a "
                "strict Q→A. Only proceed when you actually have what you need."
            ),
            resume=True,
        )
    finally:
        _active_clarification_tasks.discard(task_id)

    follow_up = _extract_clarification(result.output)
    if follow_up:
        log.info(f"Task #{task_id} needs another clarification: {follow_up[:100]}...")
        await transition_task(task_id, "awaiting_clarification", follow_up)
        await publish(
            task_clarification_needed(task_id, question=follow_up, phase="continuation")
        )
        return

    await publish(task_clarification_resolved(task_id, output=result.output))


async def _try_assign_repo(task_id: int, message: str) -> bool:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/repos")
        if resp.status_code != 200:
            return False
        repos = resp.json()
        msg_lower = message.lower()
        for repo_dict in repos:
            name = repo_dict.get("name", "")
            if name and name.lower() in msg_lower:
                resp = await client.patch(
                    f"{ORCHESTRATOR_URL}/tasks/{task_id}/repo",
                    json={"repo_name": name},
                )
                if resp.status_code == 200:
                    log.info(f"Assigned repo '{name}' to task #{task_id} from user message")
                    return True
    return False


async def handle_blocked_response(task_id: int, task: TaskData, message: str) -> None:
    """Resume a blocked task after the user provides input."""
    log.info(f"Resuming blocked task #{task_id} with user message: {message[:100]}...")

    if not task.repo_name:
        assigned = await _try_assign_repo(task_id, message)
        if not assigned:
            log.warning(f"Task #{task_id} blocked with no repo, couldn't extract from message")
            await publish(
                task_blocked(
                    task_id,
                    error="No repo assigned. Please include the repo name in your message.",
                )
            )
            return

    # Lifecycle dispatch: tell the right phase to take over. Direct calls
    # rather than bus round-trips because these are intra-handler control
    # flow (the bus is for entry from Redis Streams, not for re-routing).
    if task.pr_url:
        await transition_task(task_id, "coding", f"User unblocked: {message[:200]}")
        await review.handle_pr_review_comments(task_id, message)
    elif task.plan:
        await transition_task(task_id, "coding", f"User unblocked: {message[:200]}")
        await coding.handle_coding(task_id)
    else:
        await transition_task(task_id, "planning", f"User unblocked: {message[:200]}")
        await planning.handle_planning(task_id)


async def handle_clarification_event(event: Event) -> None:
    """EventBus entry for ``task.clarification_response``."""
    if not event.task_id:
        return
    answer = event.payload.get("answer", "") if event.payload else ""
    if answer:
        await handle_clarification_response(event.task_id, answer)


async def route_human_message(event: Event) -> None:
    """EventBus entry for ``human.message`` — routes by current task status.

    Status → action:
      awaiting_clarification              → clarification answer
      blocked                             → blocked unblock
      pr_created/awaiting_ci/
        awaiting_review/coding (with PR)  → PR review comments
      coding (no PR yet)                  → push as guidance
      awaiting_approval/planning          → plan conversation
      queued                              → emit task.start_queued
      else                                → log and drop
    """
    task_id = event.task_id
    comments = event.payload.get("message", "") if event.payload else ""
    if not task_id or not comments:
        return

    task = await get_task(task_id)
    if not task:
        return

    if task.status == "awaiting_clarification":
        await handle_clarification_response(task_id, comments)
    elif task.status == "blocked":
        await handle_blocked_response(task_id, task, comments)
    elif task.status in ("pr_created", "awaiting_ci", "awaiting_review", "coding") and task.pr_url:
        await review.handle_pr_review_comments(task_id, comments)
    elif task.status == "coding" and not task.pr_url:
        # Agent is actively coding — push as guidance for next turn
        await task_channel(task_id).push_guidance(comments)
        log.info(f"Pushed guidance to coding task #{task_id}")
    elif task.status in ("awaiting_approval", "planning"):
        # Resume the planning agent session so the user can discuss the plan
        await handle_plan_conversation(task_id, comments)
    elif task.status == "queued":
        # User wants to kick a queued task — ask orchestrator to start it
        log.info(f"Message for queued task #{task_id} — attempting to start")
        await publish(task_start_queued(task_id))
    else:
        log.info(f"Message for task #{task_id} in status '{task.status}' — not routing")
