"""Slack-DM conversational assistant.

A small Claude-powered loop that turns each Slack DM into a tool-using
agent invocation. The tools mirror auto-agent's task-management API, so
the user can say things like "create a task on cardamon to add a feedback
form" and the assistant will (after confirming) call ``create_task``.

Conversation state is per-Slack-user, kept in memory, bounded to the last
N messages, and reset after long idle. No DB persistence — if the process
restarts the user picks up a fresh thread, which is fine for chat.

The actual coding pipeline (planning, grilling, approval, coding) runs
unchanged after a task is created. This module only handles the *front
door* — turning natural language into the right API calls.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from agent.llm import get_provider
from agent.llm.types import Message, ToolCall, ToolDefinition
from shared.config import settings
from shared.events import human_message, publish

log = logging.getLogger(__name__)

ORCHESTRATOR_URL = settings.orchestrator_url

# Reset the per-user conversation if they've been silent this long.
SESSION_TTL_SECONDS = 30 * 60

# Trim the in-memory history to the last N messages so the LLM call stays
# cheap even after a long thread.
MAX_HISTORY_MESSAGES = 30

# Hard upper bound on tool-loop iterations per single user message —
# protects against pathological loops where the model keeps calling tools
# without ever emitting a final reply.
MAX_TURNS_PER_REQUEST = 8


SYSTEM_PROMPT = """\
You are auto-agent. You manage software-engineering tasks on behalf of a
small team via Slack DMs. You have tools that call the auto-agent API; \
the actual implementation work happens in a separate coding pipeline once \
a task is queued.

How to behave:
- Always check details with the user before doing anything that creates, \
  cancels, approves, or rejects work. Don't guess. If the user is vague \
  about which repo, which task, or what they want done, ask one focused \
  follow-up question.
- After running a tool, summarise the result in one or two sentences. \
  Don't paste raw JSON.
- Plain prose, friendly, brief. Skip emojis unless the user uses them.
- One question at a time when clarifying.

You can:
- List the user's tasks (filter by status if helpful).
- Read a specific task's status, plan, or PR.
- Create a new task on a named repo (after confirming the repo + the \
  description).
- Approve or reject a plan that's awaiting approval.
- Send a clarification answer to a task that's asked one.
- Cancel a running task.
- List the available repos so you can match a name the user gave you \
  (e.g. "the cardamon repo" → look it up before creating).

What you don't do: write code, run commands, or do the engineering work \
yourself. Your job is to talk to the user, gather what's needed, and call \
the right tool.\
"""


_TOOL_DEFS: list[ToolDefinition] = [
    ToolDefinition(
        name="list_my_tasks",
        description=(
            "List tasks owned by the current user. Optional status filter: "
            "'active' (anything not done/failed), 'awaiting_approval', "
            "'blocked', 'done', 'all'. Default 'active'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "active",
                        "awaiting_approval",
                        "blocked",
                        "blocked_on_auth",
                        "done",
                        "failed",
                        "all",
                    ],
                },
            },
        },
    ),
    ToolDefinition(
        name="get_task",
        description="Fetch a single task's full state (status, plan, PR, error).",
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    ),
    ToolDefinition(
        name="list_repos",
        description=(
            "List all repositories registered with auto-agent. Use this "
            "to fuzzy-match a name the user gave you against canonical "
            "repo names before calling create_task."
        ),
        parameters={"type": "object", "properties": {}},
    ),
    ToolDefinition(
        name="create_task",
        description=(
            "Queue a new task on a specific repo. Confirm the repo and "
            "the description with the user before calling this."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo_name": {
                    "type": "string",
                    "description": "Canonical repo name as returned by list_repos.",
                },
                "description": {
                    "type": "string",
                    "description": "What the user wants done. Multi-line is fine.",
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Short title (≤120 chars). If unsure, derive a "
                        "concise one from the description."
                    ),
                },
            },
            "required": ["repo_name", "description"],
        },
    ),
    ToolDefinition(
        name="approve_plan",
        description=(
            "Approve a plan that's awaiting approval. Optional feedback "
            "is forwarded to the coding agent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "feedback": {"type": "string"},
            },
            "required": ["task_id"],
        },
    ),
    ToolDefinition(
        name="reject_plan",
        description="Reject a plan with feedback explaining what to change.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "feedback": {"type": "string"},
            },
            "required": ["task_id", "feedback"],
        },
    ),
    ToolDefinition(
        name="answer_clarification",
        description=(
            "Send the user's answer to a task that has paused on a "
            "clarification question."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "answer": {"type": "string"},
            },
            "required": ["task_id", "answer"],
        },
    ),
    ToolDefinition(
        name="cancel_task",
        description="Cancel a running task. Confirm with the user first.",
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Per-user conversation state
# ---------------------------------------------------------------------------


_sessions: dict[str, dict[str, Any]] = {}


def _get_or_create_session(slack_user_id: str) -> dict[str, Any]:
    sess = _sessions.get(slack_user_id)
    now = time.time()
    if sess is None or now - sess.get("last_active", 0) > SESSION_TTL_SECONDS:
        sess = {"history": [], "last_active": now}
        _sessions[slack_user_id] = sess
    sess["last_active"] = now
    return sess


def reset_session(slack_user_id: str) -> None:
    _sessions.pop(slack_user_id, None)


# ---------------------------------------------------------------------------
# Tool dispatchers
# ---------------------------------------------------------------------------


async def _list_my_tasks(user_id: int, status: str = "active") -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/tasks")
        if resp.status_code != 200:
            return []
        tasks = resp.json()
    mine = [t for t in tasks if t.get("created_by_user_id") == user_id]
    if status == "active":
        mine = [t for t in mine if t.get("status") not in ("done", "failed")]
    elif status and status != "all":
        mine = [t for t in mine if t.get("status") == status]
    return [
        {
            "id": t["id"],
            "title": (t.get("title") or "")[:120],
            "status": t["status"],
            "repo": t.get("repo_name"),
            "pr_url": t.get("pr_url"),
        }
        for t in mine[:30]
    ]


async def _get_task(task_id: int) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}")
    if resp.status_code != 200:
        return {"error": f"task {task_id} not found"}
    t = resp.json()
    return {
        "id": t["id"],
        "title": t.get("title"),
        "description": (t.get("description") or "")[:1000],
        "status": t["status"],
        "repo": t.get("repo_name"),
        "plan": (t.get("plan") or "")[:2000],
        "pr_url": t.get("pr_url"),
        "error": (t.get("error") or "")[:500],
        "created_by_user_id": t.get("created_by_user_id"),
    }


async def _list_repos() -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/repos")
    if resp.status_code != 200:
        return []
    repos = resp.json()
    return [
        {
            "name": r["name"],
            "url": r.get("url"),
            "default_branch": r.get("default_branch"),
        }
        for r in repos
    ]


async def _create_task(
    user_id: int,
    repo_name: str,
    description: str,
    title: str | None,
) -> dict:
    payload = {
        "title": (title or description)[:120],
        "description": description,
        "source": "slack",
        "repo_name": repo_name,
        "created_by_user_id": user_id,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ORCHESTRATOR_URL}/tasks", json=payload)
    if resp.status_code != 200:
        return {
            "error": f"create_task failed: {resp.status_code} {resp.text[:200]}"
        }
    t = resp.json()
    return {"task_id": t["id"], "status": t["status"], "title": t["title"]}


async def _approve_plan(task_id: int, feedback: str = "") -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
            json={"approved": True, "feedback": feedback},
        )
    return {
        "ok": resp.status_code == 200,
        "status_code": resp.status_code,
        "body": resp.text[:200],
    }


async def _reject_plan(task_id: int, feedback: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
            json={"approved": False, "feedback": feedback},
        )
    return {
        "ok": resp.status_code == 200,
        "status_code": resp.status_code,
        "body": resp.text[:200],
    }


async def _answer_clarification(task_id: int, answer: str) -> dict:
    await publish(human_message(task_id=task_id, message=answer, source="slack"))
    return {"ok": True, "task_id": task_id}


async def _cancel_task(task_id: int) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ORCHESTRATOR_URL}/tasks/{task_id}/cancel")
    return {
        "ok": resp.status_code == 200,
        "status_code": resp.status_code,
        "body": resp.text[:200],
    }


async def _dispatch_tool(name: str, args: dict, user_id: int) -> Any:
    try:
        if name == "list_my_tasks":
            return await _list_my_tasks(user_id, args.get("status", "active"))
        if name == "get_task":
            return await _get_task(int(args["task_id"]))
        if name == "list_repos":
            return await _list_repos()
        if name == "create_task":
            return await _create_task(
                user_id,
                args["repo_name"],
                args["description"],
                args.get("title"),
            )
        if name == "approve_plan":
            return await _approve_plan(
                int(args["task_id"]), args.get("feedback", "")
            )
        if name == "reject_plan":
            return await _reject_plan(int(args["task_id"]), args["feedback"])
        if name == "answer_clarification":
            return await _answer_clarification(
                int(args["task_id"]), args["answer"]
            )
        if name == "cancel_task":
            return await _cancel_task(int(args["task_id"]))
    except KeyError as e:
        return {"error": f"missing required argument: {e}"}
    except Exception as e:
        log.exception("tool dispatch failed")
        return {"error": f"tool error: {e}"}
    return {"error": f"unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Conversation entry point
# ---------------------------------------------------------------------------


async def converse(slack_user_id: str, user_id: int, text: str, *, org_id: int | None = None) -> str:
    """Process one user DM. Returns the assistant's textual reply.

    ``org_id`` is accepted for forward-compatibility (E5 will use it to
    scope API calls to the correct organisation). For now it is a no-op.
    """
    if text.lower().strip() in ("reset", "clear"):
        reset_session(slack_user_id)
        return "Cleared our conversation. Starting fresh."

    sess = _get_or_create_session(slack_user_id)
    history: list[Message] = sess["history"]
    history.append(Message(role="user", content=text))

    if len(history) > MAX_HISTORY_MESSAGES:
        del history[: len(history) - MAX_HISTORY_MESSAGES]

    provider = get_provider(model_override="fast")  # haiku — cheap, fast

    final_text = ""
    for _turn in range(MAX_TURNS_PER_REQUEST):
        try:
            response = await provider.complete(
                messages=history,
                system=SYSTEM_PROMPT,
                tools=_TOOL_DEFS,
                max_tokens=2048,
            )
        except Exception as e:
            log.exception("slack assistant LLM call failed")
            return f"(internal error: {e})"

        history.append(response.message)

        if response.stop_reason != "tool_use" or not response.message.tool_calls:
            final_text = response.message.content or ""
            break

        # Run each tool call, append a tool-result message per call.
        for call in response.message.tool_calls:
            result = await _dispatch_tool(call.name, call.arguments, user_id)
            history.append(
                Message(
                    role="tool",
                    content=json.dumps(result, default=str)[:8000],
                    tool_call_id=call.id,
                    tool_name=call.name,
                )
            )

    if not final_text:
        final_text = (
            "I got stuck thinking about that — try rephrasing or say "
            "`reset` to start over."
        )
    return final_text
