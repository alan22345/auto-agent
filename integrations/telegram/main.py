"""Telegram integration — polls for incoming messages + sends outbound notifications.

No webhook needed. Uses Telegram Bot API getUpdates (long-polling).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from shared.config import settings
from shared.events import Event, publish
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
        await publish(Event(
            type="human.message",
            task_id=task_id,
            payload={"message": answer, "source": "telegram"},
        ))
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
# ---------------------------------------------------------------------------


NOTIFY_EVENTS = {
    "task.created",
    "task.start_planning",
    "task.start_coding",
    "task.plan_ready",
    "task.review_complete",
    "task.blocked",
    "task.failed",
    "task.done",
    "task.ci_passed",
    "task.ci_failed",
    "task.clarification_needed",
    "task.rejected",
    "task.dev_deployed",
    "task.review_comments_addressed",
    "task.dev_deploy_failed",
    "task.subtask_progress",
    "po.analysis_queued",
    "po.analysis_started",
    "po.suggestions_ready",
    "po.analysis_failed",
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
                    if event.type in NOTIFY_EVENTS:
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

    # All task-scoped notifications in this function thread back to the
    # task's chat stream via reply. Bind task_id once so every send records
    # the message_id → task_id mapping used by _handle_update.
    async def send(message: str) -> None:
        await send_telegram_async(message, task_id=event.task_id)

    if event.type == "task.created":
        await send(f"📋 *New task created*\n{task_info}")
    elif event.type == "task.start_planning":
        feedback = event.payload.get("feedback") if event.payload else None
        if feedback:
            await send(f"✏️ *Revising plan* based on your feedback\n{task_info}")
        else:
            await send(f"🔍 *Planning started*\n{task_info}")
    elif event.type == "task.rejected":
        await send(f"↩️ *Plan rejected* — revising\n{task_info}")
    elif event.type == "task.done":
        await send(f"🎉 *Task done*\n{task_info}")
    elif event.type == "task.ci_failed":
        reason = event.payload.get("reason", "")
        await send(f"❌ *CI failed* — retrying\n{task_info}\n{reason}")
    elif event.type == "task.start_coding":
        await send(f"⚡ *Coding started*\n{task_info}")
    elif event.type == "task.review_complete":
        pr_url = event.payload.get("pr_url", "")
        review = event.payload.get("review", "")
        approved = event.payload.get("approved", False)
        review_preview = review[:800] if review else ""
        if is_freeform:
            # Freeform tasks auto-merge — no human review needed.
            await send(
                f"🤖 *Independent review complete (freeform — auto-merging)*\n{task_info}\n{pr_url}\n\n"
                f"{review_preview}"
            )
        elif approved:
            await send(
                f"✅ *PR ready for your review*\n{task_info}\n{pr_url}\n\n"
                f"Independent review passed.\n{review_preview}"
            )
        else:
            fixes = event.payload.get("fixes", "")
            await send(
                f"🔧 *Review comments addressed — PR ready*\n{task_info}\n{pr_url}\n\n"
                f"Reviewer found issues, they've been fixed and pushed.\n{review_preview}"
            )
    elif event.type == "task.plan_ready":
        plan = event.payload.get("plan", "")
        plan_preview = plan[:1500] if plan else "No plan details available."
        if is_freeform:
            # Freeform tasks have their plan auto-reviewed — no human action needed.
            await send(
                f"📝 *Plan ready (freeform — auto-reviewing)*\n{task_info}\n\n{plan_preview}"
            )
        else:
            await send(
                f"*Plan ready for review.*\n{task_info}\n\n{plan_preview}\n\n"
                f"Reply to approve or provide feedback."
            )
    elif event.type == "task.clarification_needed":
        question = event.payload.get("question", "")
        await send(
            f"*Clarification needed*\n{task_info}\n\n❓ {question}\n\n"
            f"Reply with `/answer {event.task_id} <your answer>` to respond."
        )
    elif event.type == "task.blocked":
        reason = event.payload.get("error", "")
        reason_text = f"\nReason: {reason}" if reason else ""
        await send(
            f"*Task blocked* — needs your input.\n{task_info}{reason_text}\n\n"
            f"Reply with `/answer {event.task_id} <your response>` to unblock."
        )
    elif event.type == "task.failed":
        error = event.payload.get("error", "unknown")
        await send(f"*Task failed.*\n{task_info}\nError: {error}")
    elif event.type == "task.dev_deployed":
        pr_url = event.payload.get("pr_url", "")
        branch = event.payload.get("branch", "")
        deploy_output = event.payload.get("output", "")
        output_preview = deploy_output[-500:] if deploy_output else ""
        await send(
            f"🚀 *Dev deployment complete*\n{task_info}\n"
            f"Branch `{branch}` deployed to dev.\n"
            f"{pr_url}\n\n"
            f"Please review the changes and merge or request changes on the PR."
            + (f"\n\n```\n{output_preview}\n```" if output_preview else "")
        )
    elif event.type == "task.dev_deploy_failed":
        pr_url = event.payload.get("pr_url", "")
        output = event.payload.get("output", "")
        await send(
            f"❌ *Dev deployment failed*\n{task_info}\n{pr_url}\n\n{output}"
        )
    elif event.type == "task.review_comments_addressed":
        pr_url = event.payload.get("pr_url", "")
        output = event.payload.get("output", "")
        output_preview = output[:500] if output else ""
        await send(
            f"*Review comments addressed* — changes pushed.\n{task_info}\n{pr_url}"
            + (f"\n\n{output_preview}" if output_preview else "")
        )
    elif event.type == "task.subtask_progress":
        current = event.payload.get("current", "?")
        total = event.payload.get("total", "?")
        title = event.payload.get("title", "")
        status = event.payload.get("status", "")
        icon = "✅" if status == "done" else "⚙️"
        await send(f"{icon} *Subtask {current}/{total}* — {title} [{status}]\n{task_info}")
    elif event.type == "task.ci_passed":
        await send(f"*CI passed* — ready for your review.\n{task_info}")
    elif event.type == "po.analysis_queued":
        repo_name = event.payload.get("repo_name", "unknown")
        position = event.payload.get("position", "?")
        await send(f"⏳ *PO analysis queued* for `{repo_name}` (position {position})")
    elif event.type == "po.analysis_started":
        repo_name = event.payload.get("repo_name", "unknown")
        await send(f"🔄 *PO analysis started* for `{repo_name}`")
    elif event.type == "po.suggestions_ready":
        repo_name = event.payload.get("repo_name", "unknown")
        count = event.payload.get("count", 0)
        await send(f"🧠 *PO analysis complete* — {count} new suggestions for `{repo_name}`")
    elif event.type == "po.analysis_failed":
        repo_name = event.payload.get("repo_name", "unknown")
        reason = event.payload.get("reason", "")
        await send(
            f"❌ *PO analysis failed* for `{repo_name}`"
            + (f"\nReason: {reason}" if reason else "")
        )
