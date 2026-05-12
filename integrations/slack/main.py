"""Slack integration — per-user DMs.

Mirrors the structure of ``integrations/telegram/main.py``: each task
notification is routed to the *task owner's* Slack DM (looked up via
``users.slack_user_id``). System-scoped events (PO analyzer, architect)
fall back to ``settings.slack_admin_user_id``.

Inbound is Socket Mode (``slack_app_token``), so no public webhook is
needed — the bot opens an outbound WebSocket to Slack and receives
events through it. Outbound calls go to Slack's Web API directly via
``slack-bolt``'s built-in ``AsyncWebClient``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import text

from shared import installation_crypto
from shared.config import settings
from shared.database import async_session
from shared.events import (
    Event,
    POEventType,
    TaskEventType,
)
from shared.redis_client import (
    ack_event,
    ensure_stream_group,
    get_redis,
    read_events,
)
from shared.task_channel import task_id_for_slack_message
from shared.types import TaskData

log = logging.getLogger(__name__)

ORCHESTRATOR_URL = settings.orchestrator_url


# ---------------------------------------------------------------------------
# DB helpers — keep ORM access narrowly scoped, return plain dicts.
# ---------------------------------------------------------------------------


async def _user_for_slack_id(slack_user_id: str, *, org_id: int | None = None) -> dict | None:
    """Look up the auto-agent user linked to a Slack user_id.

    When ``org_id`` is set, additionally require that the user is a
    member of that org (multi-tenant safety). When ``org_id`` is None
    (legacy single-tenant path), no membership filter is applied —
    matches behaviour before Phase 3.
    """
    from sqlalchemy import select

    from shared.models import OrganizationMembership, User

    async with async_session() as session:
        if org_id is None:
            result = await session.execute(
                select(User).where(User.slack_user_id == str(slack_user_id))
            )
        else:
            result = await session.execute(
                select(User)
                .join(
                    OrganizationMembership,
                    OrganizationMembership.user_id == User.id,
                )
                .where(
                    User.slack_user_id == str(slack_user_id),
                    OrganizationMembership.org_id == org_id,
                )
            )
        user = result.scalar_one_or_none()
        if user is None:
            return None
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
        }


async def _autolink_slack_user(slack_user_id: str, *, org_id: int | None = None) -> dict | None:
    """First-DM convenience: try to match this Slack user to an auto-agent
    user by Slack handle / email local-part and link them automatically.

    Tries the following candidate names against ``users.username`` (exact,
    case-insensitive) and links on the first *unique* match where the
    target user doesn't already have a different Slack ID:
      1. Slack ``name`` (the workspace handle, e.g. ``andre``)
      2. Slack ``profile.display_name`` lowercased
      3. The local-part of the user's email address

    When ``org_id`` is set, only considers users who are already members of
    that org — prevents cross-org auto-linking in multi-tenant deployments.

    Returns the linked user dict on success, None on no-match or ambiguity.
    """
    from sqlalchemy import func, select

    from shared.models import OrganizationMembership, User

    try:
        info = await _get_app().client.users_info(user=slack_user_id)
    except Exception:
        log.exception("slack users.info lookup failed")
        return None

    profile = info.get("user", {}) or {}
    candidates: list[str] = []
    if profile.get("name"):
        candidates.append(profile["name"].lower())
    inner_profile = profile.get("profile", {}) or {}
    if inner_profile.get("display_name"):
        candidates.append(inner_profile["display_name"].lower())
    if inner_profile.get("email"):
        candidates.append(inner_profile["email"].split("@")[0].lower())

    if not candidates:
        return None

    # Dedupe while preserving order.
    seen: set[str] = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    async with async_session() as session:
        for cand in candidates:
            if org_id is None:
                result = await session.execute(
                    select(User).where(func.lower(User.username) == cand)
                )
            else:
                result = await session.execute(
                    select(User)
                    .join(
                        OrganizationMembership,
                        OrganizationMembership.user_id == User.id,
                    )
                    .where(
                        func.lower(User.username) == cand,
                        OrganizationMembership.org_id == org_id,
                    )
                )
            user = result.scalar_one_or_none()
            if user is None:
                continue
            if user.slack_user_id and user.slack_user_id != slack_user_id:
                # Username matches but that auto-agent account is already
                # linked to a *different* Slack user — refuse to silently
                # override. The new sender must use /whoami + manual link.
                log.warning(
                    "slack autolink skipped: username already bound",
                    candidate=cand,
                    existing=user.slack_user_id,
                    incoming=slack_user_id,
                )
                continue
            user.slack_user_id = slack_user_id
            await session.commit()
            log.info(
                f"Auto-linked Slack user {slack_user_id} → {user.username} "
                f"(matched on '{cand}')"
            )
            return {
                "id": user.id,
                "username": user.username,
                "display_name": user.display_name,
            }
    return None


async def _slack_user_id_for_task(task_id: int | None) -> str | None:
    """Return the slack_user_id of a task's owner, or None if not linked."""
    if task_id is None:
        return None
    from sqlalchemy import select

    from shared.database import async_session
    from shared.models import Task, User

    async with async_session() as session:
        result = await session.execute(
            select(User.slack_user_id)
            .join(Task, Task.created_by_user_id == User.id)
            .where(Task.id == task_id)
        )
        return result.scalar_one_or_none()


async def _post_task_feedback(task_id: int, content: str, sender: str) -> None:
    """POST a Slack DM message to the task's chat stream."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/messages",
                json={"content": content},
                headers={"X-Sender": sender},
            )
    except Exception:
        log.exception("Failed to post task feedback via Slack reply")


# ---------------------------------------------------------------------------
# Slack app + send helpers
# ---------------------------------------------------------------------------


_app: AsyncApp | None = None


def _get_app() -> AsyncApp:
    """Build the slack-bolt async app.

    Two modes:
      * Multi-team (default once Phase 3 is rolled out): installation
        store backed by Postgres; bot tokens resolved per-team_id.
      * Legacy single-tenant: settings.slack_bot_token only — used by
        the dev VM until the distributed app is registered.

    The mode is decided lazily on first call. Tests reset _app=None to
    rebuild with different settings.
    """
    global _app
    if _app is not None:
        return _app

    if settings.slack_bot_token:
        # Legacy path — single-workspace deploy. Keep working until the
        # distributed app is registered.
        _app = AsyncApp(token=settings.slack_bot_token)
    else:
        # Phase 3 path — distributed app. signing_secret defaults to ""
        # (not None) to satisfy slack-bolt's type check while still
        # allowing Socket Mode (no public webhook endpoint).
        from integrations.slack.installation_store import PostgresInstallationStore

        _app = AsyncApp(
            signing_secret="",
            installation_store=PostgresInstallationStore(),
        )
    return _app


async def _bot_token_for_org(org_id: int) -> str | None:
    """Decrypt and return the bot token for the given org's slack install.

    Returns None when no install exists for this org — caller falls back
    to settings.slack_bot_token (legacy single-tenant) if available."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT bot_token_enc FROM slack_installations "
                "WHERE org_id = :org_id"
            ),
            {"org_id": org_id},
        )
        row = result.first()
        if row is None:
            return None
        return await installation_crypto.decrypt(
            row.bot_token_enc, session=session
        )


async def send_slack_dm(
    slack_user_id: str,
    text: str,
    *,
    task_id: int | None = None,
    org_id: int | None = None,
) -> None:
    """Send a Slack DM to `slack_user_id` in org `org_id`'s workspace.

    Resolution:
      * If `org_id` is set and that org has an installation → per-org bot token.
      * Else if `settings.slack_bot_token` is set → legacy single-tenant.
      * Else: log and bail.
    """
    if not slack_user_id:
        return

    bot_token: str | None = None
    if org_id is not None:
        bot_token = await _bot_token_for_org(org_id)
    if bot_token is None and settings.slack_bot_token:
        bot_token = settings.slack_bot_token
    if not bot_token:
        log.info(
            "send_slack_dm_no_token org_id=%s slack_user_id=%s — "
            "org hasn't installed Slack and no legacy token configured",
            org_id, slack_user_id,
        )
        return

    try:
        client = AsyncWebClient(token=bot_token)
        open_resp = await client.conversations_open(users=slack_user_id)
        channel = open_resp["channel"]["id"]
        post_resp = await client.chat_postMessage(
            channel=channel, text=text, mrkdwn=True
        )
        ts = post_resp.get("ts")
        if task_id is not None and ts:
            from shared.task_channel import task_channel
            await task_channel(task_id).bind_slack_message(ts)
    except Exception:
        log.exception("Failed to send Slack DM")


# ---------------------------------------------------------------------------
# Inbound: Socket Mode handler
# ---------------------------------------------------------------------------


async def _org_for_team(team_id: str) -> int | None:
    """Resolve a Slack team_id to an auto-agent org_id. Returns None if
    we don't have an installation for that team."""
    from sqlalchemy import text as _t
    async with async_session() as session:
        result = await session.execute(
            _t(
                "SELECT org_id FROM slack_installations "
                "WHERE team_id = :team_id"
            ),
            {"team_id": team_id},
        )
        row = result.first()
        return row.org_id if row else None


async def _handle_dm_event(event: dict[str, Any]) -> None:
    """Route a DM message event from Slack."""
    if event.get("subtype") or event.get("bot_id"):
        return  # ignore edits, joins, the bot's own posts
    if event.get("channel_type") != "im":
        return  # we only listen to DMs, never channels
    text: str = (event.get("text") or "").strip()
    if not text:
        return

    # NEW: resolve org_id from team_id. Drop events from unknown teams
    # in multi-team mode (no install for this workspace → silent ignore).
    team_id = event.get("team") or event.get("team_id")
    org_id: int | None = None
    if team_id:
        org_id = await _org_for_team(team_id)
        if org_id is None and not settings.slack_bot_token:
            log.info("slack_event_dropped_unknown_team team_id=%s", team_id)
            return

    slack_user_id: str = event.get("user", "")
    if not slack_user_id:
        return

    # `whoami` works for any sender (so users can self-link). Slack
    # intercepts anything starting with `/` as a slash-command, so we
    # accept the bare word — the slashed form is here only for users
    # who got past Slack's interception (rare).
    if text.lower().split()[0] in ("whoami", "/whoami"):
        await send_slack_dm(
            slack_user_id,
            (
                f"Your Slack user_id is `{slack_user_id}`.\n"
                "Paste this into Settings → Slack in auto-agent to link "
                "your account."
            ),
            org_id=org_id,
        )
        return

    user = await _user_for_slack_id(slack_user_id, org_id=org_id)
    if user is None:
        # First DM from this Slack user — try to auto-link by matching
        # their Slack handle / email against an auto-agent username.
        user = await _autolink_slack_user(slack_user_id, org_id=org_id)
        if user is None:
            log.info(f"Ignoring DM from unlinked Slack user: {slack_user_id}")
            await send_slack_dm(
                slack_user_id,
                (
                    "Hi! I couldn't auto-match your Slack handle to an "
                    "auto-agent account. Send me `whoami` (no slash — Slack "
                    "intercepts those) and I'll print your Slack user ID. "
                    "Paste it into Settings → Slack to link."
                ),
                org_id=org_id,
            )
            return
        # Welcome message on successful auto-link.
        await send_slack_dm(
            slack_user_id,
            (
                f"👋 Hi *{user['display_name']}* — I auto-linked your Slack "
                f"to your auto-agent account `{user['username']}`. Just talk "
                "to me normally — ask me to create a task on a repo, check "
                "what's running, approve a plan, etc. Say `reset` any time "
                "to clear our conversation."
            ),
            org_id=org_id,
        )

    log.info(f"Slack DM from {user['username']}: {text[:80]}...")

    # Reply-threading: if the user replied in a thread we created for a
    # task notification, route it as task feedback. Skip the assistant
    # path entirely for thread replies — those are direct feedback to a
    # specific task, not a fresh conversation.
    thread_ts = event.get("thread_ts")
    if thread_ts:
        task_id = await task_id_for_slack_message(thread_ts)
        if task_id is not None:
            await _post_task_feedback(
                task_id, text, sender=f"slack:{user['username']}"
            )
            await send_slack_dm(slack_user_id, f"✉️ Sent to task #{task_id}.", org_id=org_id)
            return

    # Everything else flows through the source-agnostic messenger router,
    # which owns durable conversation state + focus.
    from orchestrator.claude_auth import resolve_home_dir
    from orchestrator.messenger_router import handle as router_handle
    from shared.database import async_session

    async def _sender(target_user_id: int, body: str) -> None:
        await send_slack_dm(slack_user_id, body, org_id=org_id)

    home_dir = await resolve_home_dir(user["id"])

    try:
        async with async_session() as db:
            await router_handle(
                session=db, source="slack",
                user_id=user["id"], text=text, thread_ts=None,
                sender=_sender, home_dir=home_dir,
            )
            await db.commit()
    except Exception:
        log.exception("messenger_router crashed")
        await send_slack_dm(slack_user_id, "(internal error)", org_id=org_id)


async def inbound_loop() -> None:
    """Start the Socket Mode handler in this asyncio loop.

    Mirrors ``integrations.telegram.main.inbound_loop`` — runs forever,
    cancellation is propagated up so the orchestrator's lifespan handler
    can shut it down cleanly.
    """
    if not settings.slack_bot_token or not settings.slack_app_token:
        log.info("Slack not configured, skipping inbound polling")
        return

    app = _get_app()

    @app.event("message")
    async def _on_message(event: dict, ack):
        await ack()
        try:
            await _handle_dm_event(event)
        except Exception:
            log.exception("Slack inbound handler crashed")

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    log.info("Slack inbound polling started (Socket Mode)")
    try:
        await handler.start_async()
    except asyncio.CancelledError:
        await handler.close_async()
        raise


# ---------------------------------------------------------------------------
# Outbound: notification fan-out (mirrors integrations.telegram.main)
# ---------------------------------------------------------------------------


def _fmt_task_created(p, info, _ff, _tid):
    return f"📋 *New task created*\n{info}"


def _fmt_task_start_planning(p, info, _ff, _tid):
    return f"🔍 *Planning started*\n{info}"


def _fmt_task_start_coding(p, info, _ff, _tid):
    return f"⚡ *Coding started*\n{info}"


def _fmt_task_done(p, info, _ff, _tid):
    return f"🎉 *Task done*\n{info}"


def _fmt_task_ci_passed(p, info, _ff, _tid):
    return f"*CI passed* — ready for review.\n{info}"


def _fmt_task_ci_failed(p, info, _ff, _tid):
    return f"❌ *CI failed* — retrying\n{info}\n{p.get('reason', '')}"


def _fmt_task_clarification(p, info, _ff, _tid):
    q = p.get("question", "")
    return f"❓ *Clarification needed*\n{info}\n\n{q}\n\n_Reply in this thread to answer._"


def _fmt_task_plan_ready(p, info, _ff, _tid):
    plan = (p.get("plan") or "")[:1500]
    return (
        f"📝 *Plan ready for approval*\n{info}\n\n{plan}\n\n"
        "_Reply `approved` or `reject: <reason>` in this thread._"
    )


def _fmt_task_review_complete(p, info, is_freeform, _tid):
    pr_url = p.get("pr_url", "")
    if is_freeform:
        return f"🤖 *Review complete (freeform — auto-merging)*\n{info}\n{pr_url}"
    return f"✅ *PR ready for your review*\n{info}\n{pr_url}"


def _fmt_task_rejected(p, info, _ff, _tid):
    return f"↩️ *Plan rejected* — revising\n{info}"


def _fmt_task_blocked(p, info, _ff, _tid):
    err = (p.get("error") or "").strip()
    return f"⛔ *Task blocked*\n{info}" + (f"\n\n{err}" if err else "")


def _fmt_task_failed(p, info, _ff, _tid):
    err = (p.get("error") or "").strip()
    return f"💥 *Task failed*\n{info}" + (f"\n\n{err[:600]}" if err else "")


def _fmt_po_suggestions_ready(p, info, _ff, _tid):
    repo = p.get("repo_name", "?")
    n = p.get("count", 0)
    return f"💡 PO analysis ready for *{repo}*: {n} suggestions"


_NOTIFICATION_FORMATTERS = {
    TaskEventType.CREATED: _fmt_task_created,
    TaskEventType.START_PLANNING: _fmt_task_start_planning,
    TaskEventType.START_CODING: _fmt_task_start_coding,
    TaskEventType.DONE: _fmt_task_done,
    TaskEventType.CI_PASSED: _fmt_task_ci_passed,
    TaskEventType.CI_FAILED: _fmt_task_ci_failed,
    TaskEventType.CLARIFICATION_NEEDED: _fmt_task_clarification,
    TaskEventType.PLAN_READY: _fmt_task_plan_ready,
    TaskEventType.REVIEW_COMPLETE: _fmt_task_review_complete,
    TaskEventType.REJECTED: _fmt_task_rejected,
    TaskEventType.BLOCKED: _fmt_task_blocked,
    TaskEventType.FAILED: _fmt_task_failed,
    POEventType.SUGGESTIONS_READY: _fmt_po_suggestions_ready,
}


async def _fetch_task_for_notification(task_id: int) -> dict | None:
    """Fetch a task from the orchestrator API for notification purposes.

    Returns a plain dict with the fields notification helpers need, or
    None when the task cannot be fetched (non-200 / network error).
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}")
            if resp.status_code == 200:
                task = TaskData.model_validate(resp.json())
                return {
                    "id": task.id,
                    "title": task.title,
                    "freeform_mode": task.freeform_mode,
                    "organization_id": task.organization_id,
                }
    except Exception:
        pass
    return None


async def _notify_task_event(event_type: str, payload: dict) -> None:
    """Handle a single task event notification (unit-testable core).

    Looks up the task (to get org_id and display info), resolves the
    target Slack user, and calls send_slack_dm with org_id set so the
    right per-workspace bot token is selected.
    """
    formatter = _NOTIFICATION_FORMATTERS.get(event_type)
    if formatter is None:
        return

    task_id: int | None = payload.get("task_id")

    task_info = ""
    is_freeform = False
    org_id: int | None = None

    if task_id is not None:
        task_data = await _fetch_task_for_notification(task_id)
        if task_data:
            task_info = f"Task #{task_data['id']}: {task_data['title'][:80]}"
            is_freeform = bool(task_data["freeform_mode"])
            org_id = task_data["organization_id"]

    target_user_id: str | None
    if task_id is not None:
        target_user_id = await _slack_user_id_for_task(task_id)
        if target_user_id is None:
            return  # owner hasn't linked Slack — skip silently
    else:
        target_user_id = settings.slack_admin_user_id or None
        if not target_user_id:
            return

    message = formatter(payload, task_info, is_freeform, task_id)
    await send_slack_dm(target_user_id, message, task_id=task_id, org_id=org_id)


async def _notify_user(event: Event) -> None:
    """Dispatch a Redis event to the appropriate Slack DM."""
    payload = event.payload or {}
    if event.task_id is not None:
        payload = dict(payload)
        payload.setdefault("task_id", event.task_id)
    await _notify_task_event(event.type, payload)


async def notification_loop() -> None:
    """Listen for events and DM the relevant user."""
    if not settings.slack_bot_token:
        log.info("Slack not configured, skipping notifications")
        return

    r = await get_redis()
    await ensure_stream_group(r)
    log.info("Slack notification loop started")

    while True:
        try:
            messages = await read_events(r, consumer="slack", count=5, block=5000)
            for msg_id, data in messages:
                try:
                    event = Event.from_redis(data)
                    if event.type in _NOTIFICATION_FORMATTERS:
                        await _notify_user(event)
                except Exception:
                    log.exception("Error processing Slack notification event")
                finally:
                    await ack_event(r, msg_id, consumer="slack")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Slack notification loop error")
            await asyncio.sleep(2)
