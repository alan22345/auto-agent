"""Web chat interface for auto-agent — serves on localhost:2020."""

from __future__ import annotations

import asyncio
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
            elif msg_type == "load_history":
                task_id = data.get("task_id")
                if task_id:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}/history")
                        if resp.status_code == 200:
                            await ws.send_json({"type": "history", "task_id": task_id, "entries": resp.json()})
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
            await ws.send_json({"type": "error", "message": "Usage: /branch <repo_name> <new_branch>"})
            return
        repo_name, new_branch = parts[1], parts[2]
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{ORCHESTRATOR_URL}/repos/{repo_name}/branch",
                json={"default_branch": new_branch},
            )
            if resp.status_code == 200:
                data = resp.json()
                await broadcast({"type": "system", "message": f"Updated **{data['repo']}** default branch: `{data['old_branch']}` → `{data['new_branch']}`"})
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
                await broadcast({"type": "system", "message": f"Task #{task.id} created: {task.title}"})
            else:
                await ws.send_json({"type": "error", "message": f"Failed to create task: {resp.text}"})
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
                    await broadcast({
                        "type": "event",
                        "event_type": event.type,
                        "task_id": event.task_id,
                        "payload": event.payload,
                        "timestamp": event.timestamp.isoformat(),
                    })

                    # Also refresh the task list for clients
                    if event.type.startswith("task."):
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(f"{ORCHESTRATOR_URL}/tasks")
                            if resp.status_code == 200:
                                tasks = [TaskData.model_validate(t).model_dump() for t in resp.json()]
                                await broadcast({"type": "task_list", "tasks": tasks})
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
