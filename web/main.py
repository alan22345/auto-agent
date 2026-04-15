"""Web chat interface for auto-agent — serves on localhost:2020."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from shared.config import settings
from shared.events import Event
from shared.logging import setup_logging
from shared.redis_client import (
    ack_event,
    ensure_stream_group,
    get_redis,
    publish_event,
    read_events,
)
from shared.types import TaskData

log = setup_logging("web-ui")

ORCHESTRATOR_URL = settings.orchestrator_url
BRANCH_NAME_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")

app = FastAPI(title="Auto-Agent Chat")

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Connected websocket clients
connected_clients: set[WebSocket] = set()


async def broadcast(message: dict) -> None:
    """Send a message to all connected websocket clients."""
    dead: set[WebSocket] = set()
    for ws in connected_clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    connected_clients.add(ws)

    # Send current task list on connect
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ORCHESTRATOR_URL}/tasks")
            if resp.status_code == 200:
                tasks = [TaskData.model_validate(t).model_dump() for t in resp.json()]
                await ws.send_json({"type": "task_list", "tasks": tasks})
    except Exception:
        log.exception("Failed to fetch initial tasks")

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "send_task":
                await _handle_create_task(ws, data)
            elif msg_type == "send_message":
                await _handle_send_message(ws, data)
            elif msg_type == "approve":
                await _handle_approve(ws, data)
            elif msg_type == "reject":
                await _handle_reject(ws, data)
            elif msg_type == "mark_done":
                await _handle_mark_done(ws, data)
            elif msg_type == "approve_suggestion":
                await _handle_approve_suggestion(ws, data)
            elif msg_type == "reject_suggestion":
                await _handle_reject_suggestion(ws, data)
            elif msg_type == "promote_task":
                await _handle_promote_task(ws, data)
            elif msg_type == "revert_task":
                await _handle_revert_task(ws, data)
            elif msg_type == "toggle_freeform":
                await _handle_toggle_freeform(ws, data)
            elif msg_type == "trigger_analysis":
                await _handle_trigger_analysis(ws, data)
            elif msg_type == "create_repo":
                await _handle_create_repo(ws, data)
            elif msg_type == "load_suggestions":
                await _handle_load_suggestions(ws, data)
            elif msg_type == "load_freeform_tasks":
                await _handle_load_freeform_tasks(ws, data)
            elif msg_type == "load_freeform_config":
                await _handle_load_freeform_config(ws, data)
            elif msg_type == "load_history":
                task_id = data.get("task_id")
                if task_id:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}/history")
                        if resp.status_code == 200:
                            await ws.send_json(
                                {"type": "history", "task_id": task_id, "entries": resp.json()}
                            )
            elif msg_type == "refresh":
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{ORCHESTRATOR_URL}/tasks")
                    if resp.status_code == 200:
                        tasks = [TaskData.model_validate(t).model_dump() for t in resp.json()]
                        await ws.send_json({"type": "task_list", "tasks": tasks})

    except WebSocketDisconnect:
        connected_clients.discard(ws)


async def _handle_create_task(ws: WebSocket, data: dict) -> None:
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    repo_name = data.get("repo_name", "").strip() or None

    if not title:
        await ws.send_json({"type": "error", "message": "Task title is required"})
        return
    if len(title) > 256:
        await ws.send_json(
            {"type": "error", "message": "Task title must be 256 characters or fewer"}
        )
        return
    if len(description) > 10000:
        await ws.send_json(
            {"type": "error", "message": "Task description must be 10,000 characters or fewer"}
        )
        return

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/tasks",
            json={
                "title": title,
                "description": description,
                "source": "manual",
                "repo_name": repo_name,
            },
        )
        if resp.status_code == 200:
            task = TaskData.model_validate(resp.json())
            await broadcast({"type": "system", "message": f"Task #{task.id} created: {task.title}"})
        else:
            await ws.send_json({"type": "error", "message": f"Failed to create task: {resp.text}"})


async def _handle_send_message(ws: WebSocket, data: dict) -> None:
    text = data.get("message", "").strip()
    task_id = data.get("task_id")

    if not text:
        return

    # Handle slash commands
    if text.startswith("/branch "):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await ws.send_json(
                {"type": "error", "message": "Usage: /branch <repo_name> <new_branch>"}
            )
            return
        repo_name, new_branch = parts[1], parts[2]
        if not BRANCH_NAME_RE.match(new_branch):
            await ws.send_json(
                {
                    "type": "error",
                    "message": "Invalid branch name: only alphanumeric, '.', '_', '/', '-' allowed",
                }
            )
            return
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{ORCHESTRATOR_URL}/repos/{repo_name}/branch",
                json={"default_branch": new_branch},
            )
            if resp.status_code == 200:
                data = resp.json()
                await broadcast(
                    {
                        "type": "system",
                        "message": f"Updated **{data['repo']}** default branch: `{data['old_branch']}` → `{data['new_branch']}`",
                    }
                )
            else:
                await ws.send_json({"type": "error", "message": f"Failed: {resp.text[:200]}"})
        return

    # If no task is selected, auto-create a task from the message
    if not task_id:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/tasks",
                json={
                    "title": text[:120],
                    "description": text,
                    "source": "manual",
                },
            )
            if resp.status_code == 200:
                task = TaskData.model_validate(resp.json())
                await broadcast(
                    {"type": "system", "message": f"Task #{task.id} created: {task.title}"}
                )
            else:
                await ws.send_json(
                    {"type": "error", "message": f"Failed to create task: {resp.text}"}
                )
        return

    r = await get_redis()
    event = Event(
        type="human.message",
        task_id=task_id,
        payload={"message": text, "source": "web"},
    )
    await publish_event(r, event.to_redis())
    await r.aclose()

    await broadcast({"type": "user", "message": text})


async def _handle_approve(ws: WebSocket, data: dict) -> None:
    task_id = data.get("task_id")
    if not task_id:
        return

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
            json={"approved": True},
        )
        if resp.status_code == 200:
            await broadcast({"type": "system", "message": f"Task #{task_id} approved"})
        else:
            await ws.send_json({"type": "error", "message": f"Failed to approve: {resp.text}"})


async def _handle_mark_done(ws: WebSocket, data: dict) -> None:
    task_id = data.get("task_id")
    if not task_id:
        return

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ORCHESTRATOR_URL}/tasks/{task_id}/done")
        if resp.status_code == 200:
            await broadcast({"type": "system", "message": f"Task #{task_id} marked as done"})
        else:
            await ws.send_json({"type": "error", "message": f"Failed to mark done: {resp.text}"})


async def _handle_reject(ws: WebSocket, data: dict) -> None:
    task_id = data.get("task_id")
    feedback = data.get("feedback", "")
    if not task_id:
        return

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
            json={"approved": False, "feedback": feedback},
        )
        if resp.status_code == 200:
            await broadcast({"type": "system", "message": f"Task #{task_id} rejected"})
        else:
            await ws.send_json({"type": "error", "message": f"Failed to reject: {resp.text}"})


# --- Freeform handlers ---


async def _handle_approve_suggestion(ws: WebSocket, data: dict) -> None:
    suggestion_id = data.get("suggestion_id")
    if not suggestion_id:
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ORCHESTRATOR_URL}/suggestions/{suggestion_id}/approve")
        if resp.status_code == 200:
            task = resp.json()
            await broadcast(
                {
                    "type": "system",
                    "message": f"Suggestion #{suggestion_id} approved -> Task #{task.get('id')}",
                }
            )
        else:
            await ws.send_json(
                {"type": "error", "message": f"Failed to approve suggestion: {resp.text[:200]}"}
            )


async def _handle_reject_suggestion(ws: WebSocket, data: dict) -> None:
    suggestion_id = data.get("suggestion_id")
    if not suggestion_id:
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ORCHESTRATOR_URL}/suggestions/{suggestion_id}/reject")
        if resp.status_code == 200:
            await broadcast({"type": "system", "message": f"Suggestion #{suggestion_id} rejected"})
        else:
            await ws.send_json(
                {"type": "error", "message": f"Failed to reject suggestion: {resp.text[:200]}"}
            )


async def _handle_promote_task(ws: WebSocket, data: dict) -> None:
    task_id = data.get("task_id")
    if not task_id:
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ORCHESTRATOR_URL}/freeform/{task_id}/promote")
        if resp.status_code == 200:
            result = resp.json()
            await broadcast(
                {
                    "type": "system",
                    "message": f"Promoted task #{task_id} to main: {result.get('pr_url', '')}",
                }
            )
        else:
            await ws.send_json(
                {"type": "error", "message": f"Failed to promote: {resp.text[:200]}"}
            )


async def _handle_revert_task(ws: WebSocket, data: dict) -> None:
    task_id = data.get("task_id")
    if not task_id:
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ORCHESTRATOR_URL}/freeform/{task_id}/revert")
        if resp.status_code == 200:
            result = resp.json()
            await broadcast(
                {
                    "type": "system",
                    "message": f"Reverted task #{task_id}: {result.get('pr_url', '')}",
                }
            )
        else:
            await ws.send_json({"type": "error", "message": f"Failed to revert: {resp.text[:200]}"})


async def _handle_toggle_freeform(ws: WebSocket, data: dict) -> None:
    repo_name = data.get("repo_name", "").strip()
    enabled = data.get("enabled", True)
    dev_branch = data.get("dev_branch", "dev")
    analysis_cron = data.get("analysis_cron", "0 9 * * 1")
    auto_approve_suggestions = data.get("auto_approve_suggestions", False)
    auto_start_tasks = data.get("auto_start_tasks", False)
    if not repo_name:
        await ws.send_json({"type": "error", "message": "repo_name is required"})
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/freeform/config",
            json={
                "repo_name": repo_name,
                "enabled": enabled,
                "dev_branch": dev_branch,
                "analysis_cron": analysis_cron,
                "auto_approve_suggestions": auto_approve_suggestions,
                "auto_start_tasks": auto_start_tasks,
            },
        )
        if resp.status_code == 200:
            state = "enabled" if enabled else "disabled"
            await broadcast({"type": "system", "message": f"Freeform mode {state} for {repo_name}"})
            # Push refreshed config list to all clients
            list_resp = await client.get(f"{ORCHESTRATOR_URL}/freeform/config")
            if list_resp.status_code == 200:
                await broadcast({"type": "freeform_config_list", "configs": list_resp.json()})
        else:
            await ws.send_json({"type": "error", "message": f"Failed: {resp.text[:200]}"})


async def _handle_create_repo(ws: WebSocket, data: dict) -> None:
    description = data.get("description", "").strip()
    org = data.get("org", "").strip()
    # The "loop" toggle from the UI controls whether the new repo enters the
    # continuous-improvement loop after scaffolding. When False, freeform mode
    # is still enabled but auto_approve_suggestions is left off so the PO won't
    # turn its suggestions into tasks without a human click.
    loop = data.get("loop", True)
    if not description:
        await ws.send_json({"type": "error", "message": "Description is required"})
        return
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/freeform/create-repo",
            json={"description": description, "org": org, "private": True, "loop": loop},
        )
        if resp.status_code == 200:
            payload = resp.json()
            repo = payload.get("repo", {})
            task = payload.get("task", {})
            await broadcast(
                {
                    "type": "system",
                    "message": f"Created repo {repo.get('name')} ({repo.get('url')}) and queued scaffold task #{task.get('id')}",
                }
            )
            await ws.send_json({"type": "repo_created", "repo": repo, "task": task})
        else:
            await ws.send_json({"type": "error", "message": f"Failed: {resp.text[:300]}"})


async def _handle_trigger_analysis(ws: WebSocket, data: dict) -> None:
    repo_name = data.get("repo_name", "").strip()
    if not repo_name:
        await ws.send_json({"type": "error", "message": "repo_name is required"})
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{ORCHESTRATOR_URL}/freeform/analyze/{repo_name}")
        if resp.status_code == 200:
            await broadcast({"type": "system", "message": f"PO analysis triggered for {repo_name}"})
        else:
            await ws.send_json({"type": "error", "message": f"Failed: {resp.text[:200]}"})


async def _handle_load_suggestions(ws: WebSocket, data: dict) -> None:
    status = data.get("status", "")
    repo_name = data.get("repo_name", "")
    params = {}
    if status:
        params["status"] = status
    if repo_name:
        params["repo_name"] = repo_name
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/suggestions", params=params)
        if resp.status_code == 200:
            await ws.send_json({"type": "suggestion_list", "suggestions": resp.json()})


async def _handle_load_freeform_tasks(ws: WebSocket, data: dict) -> None:
    """Load completed freeform tasks for the dev changes panel."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/tasks")
        if resp.status_code == 200:
            all_tasks = resp.json()
            freeform_tasks = [t for t in all_tasks if t.get("freeform_mode")]
            await ws.send_json({"type": "freeform_task_list", "tasks": freeform_tasks})


async def _handle_load_freeform_config(ws: WebSocket, data: dict) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/freeform/config")
        if resp.status_code == 200:
            await ws.send_json({"type": "freeform_config_list", "configs": resp.json()})


# --- Event listener: push task updates to the web UI ---


async def event_listener() -> None:
    """Listen for events on Redis and push updates to connected websocket clients."""
    r = await get_redis()
    await ensure_stream_group(r)
    log.info("Web UI event listener started")

    while True:
        try:
            messages = await read_events(r, consumer="web-ui", count=10, block=3000)
            for msg_id, data in messages:
                try:
                    event = Event.from_redis(data)
                    # Push every event to the web UI
                    await broadcast(
                        {
                            "type": "event",
                            "event_type": event.type,
                            "task_id": event.task_id,
                            "payload": event.payload,
                            "timestamp": event.timestamp.isoformat(),
                        }
                    )

                    # Also refresh the task list for clients
                    if event.type.startswith("task."):
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(f"{ORCHESTRATOR_URL}/tasks")
                            if resp.status_code == 200:
                                tasks = [
                                    TaskData.model_validate(t).model_dump() for t in resp.json()
                                ]
                                await broadcast({"type": "task_list", "tasks": tasks})

                    # Refresh suggestions and configs when PO analysis completes
                    if event.type == "po.suggestions_ready":
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(
                                f"{ORCHESTRATOR_URL}/suggestions", params={"status": "pending"}
                            )
                            if resp.status_code == 200:
                                await broadcast(
                                    {"type": "suggestion_list", "suggestions": resp.json()}
                                )
                            # Refresh freeform configs so last_analysis_at updates in UI
                            cfg_resp = await client.get(f"{ORCHESTRATOR_URL}/freeform/config")
                            if cfg_resp.status_code == 200:
                                await broadcast(
                                    {"type": "freeform_config_list", "configs": cfg_resp.json()}
                                )
                except Exception:
                    log.exception("Error processing web event")
                finally:
                    await ack_event(r, msg_id, consumer="web-ui")
        except Exception:
            log.exception("Web event listener error")
            await asyncio.sleep(2)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(event_listener())


if __name__ == "__main__":
    uvicorn.run("web.main:app", host="0.0.0.0", port=2020, reload=True)
