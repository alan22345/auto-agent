"""Telegram integration — polls for incoming messages + sends outbound notifications.

No webhook needed. Uses Telegram Bot API getUpdates (long-polling).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx

from shared.config import settings
from shared.events import (
    Event,
    POEventType,
    TaskEventType,
    human_message,
    publish,
)
from shared.notifier import send_telegram_async
from shared.redis_client import (
    ack_event,
    ensure_stream_group,
    get_redis,
    read_events,
)
from shared.task_channel import task_id_for_telegram_message
from shared.types import TaskData

log = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
ORCHESTRATOR_URL = settings.orchestrator_url


async def _post_task_feedback(task_id: int, content: str, sender: str) -> None:
    """POST a user message to the task's chat stream via the orchestrator API."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/messages",
                json={"content": content},
                headers={"X-Sender": sender},
            )
    except Exception:
        log.exception("Failed to post task feedback via Telegram reply")


# ---------------------------------------------------------------------------
# Inbound: poll Telegram for new messages
# ---------------------------------------------------------------------------


async def inbound_loop() -> None:
    """Long-poll Telegram for incoming messages from the user."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("Telegram not configured, skipping inbound polling")
        return

    # Clear any pending webhook so getUpdates works
    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API}/deleteWebhook")

    offset = 0
    log.info("Telegram inbound polling started")

    while True:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    f"{TELEGRAM_API}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                )
                if not resp.is_success:
                    log.warning(f"Telegram getUpdates error: {resp.status_code}")
                    await asyncio.sleep(5)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    await _handle_update(update)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram inbound polling error")
            await asyncio.sleep(5)


async def _handle_update(update: dict[str, Any]) -> None:
    """Process a single Telegram update."""
    message = update.get("message", {})
    text: str = message.get("text", "").strip()
    if not text:
        return

    chat_id = str(message.get("chat", {}).get("id", ""))

    # Only respond to the configured user
    if chat_id != settings.telegram_chat_id:
        log.warning(f"Ignoring message from unknown chat_id: {chat_id}")
        return

    log.info(f"Telegram message: {text[:80]}...")

    # Reply-threading: if the user replied to a notification we sent, and
    # that notification was tagged with a task_id, route the reply into
    # the task's message stream as user feedback.
    reply_to = message.get("reply_to_message") or {}
    reply_msg_id = reply_to.get("message_id")
    if reply_msg_id and not text.startswith("/"):
        task_id = await task_id_for_telegram_message(reply_msg_id)
        if task_id is not None:
            await _post_task_feedback(task_id, text, sender=f"telegram:{chat_id}")
            await send_telegram_async(f"✉️ Sent to task #{task_id}.")
            return

    if text.startswith("/"):
        await _handle_command(text)
        return

    # Check if user is approving a plan (e.g. "approved", "approve", "lgtm")
    lower = text.lower().strip()
    if lower in ("approved", "approve", "lgtm", "yes", "ok"):
        await _handle_approval(approved=True)
        return

    # Check if user is rejecting with feedback (e.g. "reject: needs more detail")
    if lower.startswith("reject"):
        feedback = text[len("reject"):].lstrip(": ").strip()
        await _handle_approval(approved=False, feedback=feedback)
        return

    # Any other free-text message creates a task
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/tasks",
                json={
                    "title": text[:120],
                    "description": text,
                    "source": "telegram",
                },
            )
            if resp.status_code == 200:
                task = TaskData.model_validate(resp.json())
                await send_telegram_async(f"Task #{task.id} created: {task.title[:80]}")
            else:
                await send_telegram_async(f"Failed to create task: {resp.text[:200]}")
    except Exception:
        log.exception("Failed to create task from Telegram")
        await send_telegram_async("Error creating task.")


async def _handle_approval(approved: bool = True, feedback: str = "") -> None:
    """Find the task awaiting approval and approve/reject it."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ORCHESTRATOR_URL}/tasks")
            tasks = [TaskData.model_validate(t) for t in resp.json()]
            awaiting = [t for t in tasks if t.status == "awaiting_approval"]

            if not awaiting:
                await send_telegram_async("No tasks awaiting approval.")
                return

            task = awaiting[0]  # Approve the most recent one
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task.id}/approve",
                json={"approved": approved, "feedback": feedback},
            )
            if resp.status_code == 200:
                action = "Approved" if approved else "Rejected"
                await send_telegram_async(f"{action} task #{task.id}: {task.title[:80]}")
            else:
                await send_telegram_async(f"Failed to approve: {resp.text[:200]}")
    except Exception:
        log.exception("Error handling approval")
        await send_telegram_async("Error processing approval.")


async def _handle_command(text: str) -> None:
    """Process Telegram commands."""
    cmd = text.lower().split()[0].split("@")[0]

    if cmd == "/status":
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{ORCHESTRATOR_URL}/tasks")
                tasks_raw = resp.json()
            tasks = [TaskData.model_validate(t) for t in tasks_raw]
            active = [t for t in tasks if t.status not in ("done", "failed")]
            if active:
                lines = [f"#{t.id} [{t.status}] {t.title[:60]}" for t in active[:5]]
                await send_telegram_async("*Active tasks:*\n" + "\n".join(lines))
            else:
                await send_telegram_async("No active tasks.")
        except Exception:
            log.exception("Error fetching tasks for /status")
            await send_telegram_async("Error fetching tasks.")

    elif cmd == "/answer":
        # /answer <task_id> <response text>
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await send_telegram_async("Usage: `/answer <task_id> <your answer>`")
            return
        try:
            task_id = int(parts[1])
        except ValueError:
            await send_telegram_async("Invalid task ID. Usage: `/answer <task_id> <your answer>`")
            return
        answer = parts[2]
        await publish(human_message(task_id=task_id, message=answer, source="telegram"))
        await send_telegram_async(f"Answer sent to task #{task_id}.")

    elif cmd == "/cancel":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await send_telegram_async("Usage: `/cancel <task_id>`")
            return
        try:
            task_id = int(parts[1])
        except ValueError:
            await send_telegram_async("Invalid task ID.")
            return
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{ORCHESTRATOR_URL}/tasks/{task_id}/cancel")
            if resp.status_code == 200:
                await send_telegram_async(f"Task #{task_id} cancelled.")
            else:
                await send_telegram_async(f"Failed: {resp.text[:200]}")

    elif cmd == "/delete":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await send_telegram_async("Usage: `/delete <task_id>`")
            return
        try:
            task_id = int(parts[1])
        except ValueError:
            await send_telegram_async("Invalid task ID.")
            return
        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{ORCHESTRATOR_URL}/tasks/{task_id}")
            if resp.status_code == 200:
                await send_telegram_async(f"Task #{task_id} deleted.")
            else:
                await send_telegram_async(f"Failed: {resp.text[:200]}")

    elif cmd == "/branch":
        # /branch <repo_name> <branch>
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await send_telegram_async("Usage: `/branch <repo_name> <new_branch>`\nExample: `/branch cardamon prod`")
            return
        repo_name = parts[1]
        new_branch = parts[2]
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{ORCHESTRATOR_URL}/repos/{repo_name}/branch",
                json={"default_branch": new_branch},
            )
            if resp.status_code == 200:
                data = resp.json()
                await send_telegram_async(
                    f"Updated *{data['repo']}* default branch: `{data['old_branch']}` → `{data['new_branch']}`"
                )
            else:
                await send_telegram_async(f"Failed: {resp.text[:200]}")

    elif cmd == "/done":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await send_telegram_async("Usage: `/done <task_id>`")
            return
        try:
            task_id = int(parts[1])
        except ValueError:
            await send_telegram_async("Invalid task ID.")
            return
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{ORCHESTRATOR_URL}/tasks/{task_id}/done")
            if resp.status_code == 200:
                await send_telegram_async(f"Task #{task_id} marked as done.")
            else:
                await send_telegram_async(f"Failed: {resp.text[:200]}")

    elif cmd == "/newrepo":
        # /newrepo <description>
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await send_telegram_async(
                "Usage: `/newrepo <description>`\n"
                "Example: `/newrepo a Next.js todo app with dark mode`"
            )
            return
        description = parts[1]
        await send_telegram_async("Creating repo... (this can take ~30s)")
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/freeform/create-repo",
                json={"description": description, "private": True},
            )
            if resp.status_code == 200:
                payload = resp.json()
                repo = payload.get("repo", {})
                task = payload.get("task", {})
                await send_telegram_async(
                    f"Created *{repo.get('name')}*\n"
                    f"{repo.get('url')}\n\n"
                    f"Scaffold task #{task.get('id')} queued."
                )
            else:
                await send_telegram_async(f"Failed: {resp.text[:300]}")

    elif cmd == "/freeform":
        # /freeform <repo_name> [on|off]
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            await send_telegram_async(
                "Usage: `/freeform <repo_name> [on|off]`\n"
                "Example: `/freeform synapse-common` (enables)\n"
                "         `/freeform synapse-common off` (disables)"
            )
            return
        repo_name = parts[1]
        toggle = parts[2].lower() if len(parts) > 2 else "on"
        if toggle not in ("on", "off"):
            await send_telegram_async("Toggle must be `on` or `off`.")
            return
        enabled = toggle == "on"
        async with httpx.AsyncClient() as client:
            # Fetch existing config (if any) so we don't clobber dev_branch /
            # analysis_cron when just toggling enabled.
            list_resp = await client.get(f"{ORCHESTRATOR_URL}/freeform/config")
            existing = None
            if list_resp.status_code == 200:
                for cfg in list_resp.json():
                    if cfg.get("repo_name") == repo_name:
                        existing = cfg
                        break

            payload = {
                "repo_name": repo_name,
                "enabled": enabled,
                "dev_branch": existing["dev_branch"] if existing else "dev",
                "analysis_cron": existing["analysis_cron"] if existing else "0 9 * * 1",
            }
            resp = await client.post(f"{ORCHESTRATOR_URL}/freeform/config", json=payload)
            if resp.status_code == 200:
                state = "enabled" if enabled else "disabled"
                await send_telegram_async(f"Freeform mode *{state}* for `{repo_name}`.")
            else:
                await send_telegram_async(f"Failed: {resp.text[:200]}")

    elif cmd == "/help":
        await send_telegram_async(
            "*Available commands:*\n"
            "/status — show active tasks\n"
            "/done <task\\_id> — mark a task as done (approve)\n"
            "/cancel <task\\_id> — cancel a running task\n"
            "/delete <task\\_id> — permanently delete a task\n"
            "/answer <task\\_id> <response> — answer a clarification question\n"
            "/branch <repo> <branch> — change a repo's default branch\n"
            "/freeform <repo> \\[on|off] — enable/disable freeform mode\n"
            "/newrepo <description> — create a new repo and scaffold it from scratch\n"
            "/help — show this message"
        )

    else:
        await send_telegram_async(f"Unknown command: {cmd}\nType /help for available commands.")


# ---------------------------------------------------------------------------
# Outbound: listen for events and notify via Telegram
#
# `_NOTIFICATION_FORMATTERS` is keyed on TaskEventType / POEventType members
# and is the single place that decides which events trigger a Telegram
# message. Each formatter receives the event payload plus the resolved
# task context (task_info string + is_freeform flag) and returns the
# message body. Adding a new event-type notification is one entry.
# ---------------------------------------------------------------------------


def _fmt_task_created(payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    return f"📋 *New task created*\n{task_info}"


def _fmt_task_start_planning(payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    feedback = payload.get("feedback")
    if feedback:
        return f"✏️ *Revising plan* based on your feedback\n{task_info}"
    return f"🔍 *Planning started*\n{task_info}"


def _fmt_task_start_coding(_payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    return f"⚡ *Coding started*\n{task_info}"


def _fmt_task_rejected(_payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    return f"↩️ *Plan rejected* — revising\n{task_info}"


def _fmt_task_done(_payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    return f"🎉 *Task done*\n{task_info}"


def _fmt_task_ci_passed(_payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    return f"*CI passed* — ready for your review.\n{task_info}"


def _fmt_task_ci_failed(payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    reason = payload.get("reason", "")
    return f"❌ *CI failed* — retrying\n{task_info}\n{reason}"


def _fmt_task_review_complete(payload: dict[str, Any], task_info: str, is_freeform: bool, _task_id: int | None) -> str:
    pr_url = payload.get("pr_url", "")
    review = payload.get("review", "")
    approved = payload.get("approved", False)
    review_preview = review[:800] if review else ""
    if is_freeform:
        return (
            f"🤖 *Independent review complete (freeform — auto-merging)*\n{task_info}\n{pr_url}\n\n"
            f"{review_preview}"
        )
    if approved:
        return (
            f"✅ *PR ready for your review*\n{task_info}\n{pr_url}\n\n"
            f"Independent review passed.\n{review_preview}"
        )
    return (
        f"🔧 *Review comments addressed — PR ready*\n{task_info}\n{pr_url}\n\n"
        f"Reviewer found issues, they've been fixed and pushed.\n{review_preview}"
    )


def _fmt_task_plan_ready(payload: dict[str, Any], task_info: str, is_freeform: bool, _task_id: int | None) -> str:
    plan = payload.get("plan", "")
    plan_preview = plan[:1500] if plan else "No plan details available."
    if is_freeform:
        return f"📝 *Plan ready (freeform — auto-reviewing)*\n{task_info}\n\n{plan_preview}"
    return (
        f"*Plan ready for review.*\n{task_info}\n\n{plan_preview}\n\n"
        f"Reply to approve or provide feedback."
    )


def _fmt_task_clarification_needed(payload: dict[str, Any], task_info: str, _is_freeform: bool, task_id: int | None) -> str:
    question = payload.get("question", "")
    return (
        f"*Clarification needed*\n{task_info}\n\n❓ {question}\n\n"
        f"Reply with `/answer {task_id} <your answer>` to respond."
    )


def _fmt_task_blocked(payload: dict[str, Any], task_info: str, _is_freeform: bool, task_id: int | None) -> str:
    reason = payload.get("error", "")
    reason_text = f"\nReason: {reason}" if reason else ""
    return (
        f"*Task blocked* — needs your input.\n{task_info}{reason_text}\n\n"
        f"Reply with `/answer {task_id} <your response>` to unblock."
    )


def _fmt_task_failed(payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    error = payload.get("error", "unknown")
    return f"*Task failed.*\n{task_info}\nError: {error}"


def _fmt_task_dev_deployed(payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    pr_url = payload.get("pr_url", "")
    branch = payload.get("branch", "")
    deploy_output = payload.get("output", "")
    output_preview = deploy_output[-500:] if deploy_output else ""
    return (
        f"🚀 *Dev deployment complete*\n{task_info}\n"
        f"Branch `{branch}` deployed to dev.\n"
        f"{pr_url}\n\n"
        f"Please review the changes and merge or request changes on the PR."
        + (f"\n\n```\n{output_preview}\n```" if output_preview else "")
    )


def _fmt_task_dev_deploy_failed(payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    pr_url = payload.get("pr_url", "")
    output = payload.get("output", "")
    return f"❌ *Dev deployment failed*\n{task_info}\n{pr_url}\n\n{output}"


def _fmt_task_review_comments_addressed(payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    pr_url = payload.get("pr_url", "")
    output = payload.get("output", "")
    output_preview = output[:500] if output else ""
    return (
        f"*Review comments addressed* — changes pushed.\n{task_info}\n{pr_url}"
        + (f"\n\n{output_preview}" if output_preview else "")
    )


def _fmt_task_subtask_progress(payload: dict[str, Any], task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    current = payload.get("current", "?")
    total = payload.get("total", "?")
    title = payload.get("title", "")
    status = payload.get("status", "")
    icon = "✅" if status == "done" else "⚙️"
    return f"{icon} *Subtask {current}/{total}* — {title} [{status}]\n{task_info}"


def _fmt_po_analysis_queued(payload: dict[str, Any], _task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    repo_name = payload.get("repo_name", "unknown")
    position = payload.get("position", "?")
    return f"⏳ *PO analysis queued* for `{repo_name}` (position {position})"


def _fmt_po_analysis_started(payload: dict[str, Any], _task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    repo_name = payload.get("repo_name", "unknown")
    return f"🔄 *PO analysis started* for `{repo_name}`"


def _fmt_po_suggestions_ready(payload: dict[str, Any], _task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    repo_name = payload.get("repo_name", "unknown")
    count = payload.get("count", 0)
    return f"🧠 *PO analysis complete* — {count} new suggestions for `{repo_name}`"


def _fmt_po_analysis_failed(payload: dict[str, Any], _task_info: str, _is_freeform: bool, _task_id: int | None) -> str:
    repo_name = payload.get("repo_name", "unknown")
    reason = payload.get("reason", "")
    return f"❌ *PO analysis failed* for `{repo_name}`" + (
        f"\nReason: {reason}" if reason else ""
    )


# `Formatter` takes (payload, task_info, is_freeform, task_id) and returns the
# rendered Telegram message. Pinning the signature keeps the typo guarantee on
# the consumer side too: a formatter with a wrong shape fails at registration.
Formatter = Callable[[dict[str, Any], str, bool, int | None], str]


# Map of event-type wire string → formatter. Keyed on the StrEnum value
# (which is the wire string) so a new event type can be hooked up by adding
# a single line. Events not in this dict are silently dropped — that is the
# legacy behaviour and the unit tests below pin it.
_NOTIFICATION_FORMATTERS: dict[str, Formatter] = {
    TaskEventType.CREATED: _fmt_task_created,
    TaskEventType.START_PLANNING: _fmt_task_start_planning,
    TaskEventType.START_CODING: _fmt_task_start_coding,
    TaskEventType.PLAN_READY: _fmt_task_plan_ready,
    TaskEventType.REVIEW_COMPLETE: _fmt_task_review_complete,
    TaskEventType.BLOCKED: _fmt_task_blocked,
    TaskEventType.FAILED: _fmt_task_failed,
    TaskEventType.DONE: _fmt_task_done,
    TaskEventType.CI_PASSED: _fmt_task_ci_passed,
    TaskEventType.CI_FAILED: _fmt_task_ci_failed,
    TaskEventType.CLARIFICATION_NEEDED: _fmt_task_clarification_needed,
    TaskEventType.REJECTED: _fmt_task_rejected,
    TaskEventType.DEV_DEPLOYED: _fmt_task_dev_deployed,
    TaskEventType.REVIEW_COMMENTS_ADDRESSED: _fmt_task_review_comments_addressed,
    TaskEventType.DEV_DEPLOY_FAILED: _fmt_task_dev_deploy_failed,
    TaskEventType.SUBTASK_PROGRESS: _fmt_task_subtask_progress,
    POEventType.ANALYSIS_QUEUED: _fmt_po_analysis_queued,
    POEventType.ANALYSIS_STARTED: _fmt_po_analysis_started,
    POEventType.SUGGESTIONS_READY: _fmt_po_suggestions_ready,
    POEventType.ANALYSIS_FAILED: _fmt_po_analysis_failed,
}


async def notification_loop() -> None:
    """Listen for events and send Telegram notifications when user input is needed."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("Telegram not configured, skipping notifications")
        return

    r = await get_redis()
    await ensure_stream_group(r)
    log.info("Telegram notification loop started")

    while True:
        try:
            messages = await read_events(r, consumer="telegram", count=5, block=5000)
            for msg_id, data in messages:
                try:
                    event = Event.from_redis(data)
                    if event.type in _NOTIFICATION_FORMATTERS:
                        await _notify_user(event)
                except Exception:
                    log.exception("Error processing notification event")
                finally:
                    await ack_event(r, msg_id, consumer="telegram")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Notification loop error")
            await asyncio.sleep(2)


async def _notify_user(event: Event) -> None:
    """Send a Telegram notification based on event type."""
    formatter = _NOTIFICATION_FORMATTERS.get(event.type)
    if formatter is None:
        return

    task_info = ""
    is_freeform = False
    if event.task_id:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{ORCHESTRATOR_URL}/tasks/{event.task_id}")
                if resp.status_code == 200:
                    task = TaskData.model_validate(resp.json())
                    task_info = f"Task #{task.id}: {task.title[:80]}"
                    is_freeform = bool(task.freeform_mode)
        except Exception:
            pass

    message = formatter(event.payload or {}, task_info, is_freeform, event.task_id)
    await send_telegram_async(message, task_id=event.task_id)
