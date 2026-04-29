"""Web chat interface for auto-agent — serves on localhost:2020."""

from __future__ import annotations

import asyncio
import re
import time as _time
import uuid as _uuid
from dataclasses import dataclass, field
from io import BytesIO as _BytesIO
from pathlib import Path

import httpx
import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agent.memory_extractor import extract
from shared.config import settings
from shared.events import Event
from shared.logging import setup_logging
from shared.memory_io import correct_fact, recall_entity, remember_row
from shared.redis_client import (
    ack_event,
    ensure_stream_group,
    get_redis,
    publish_event,
    read_events,
)
from shared.types import MemorySaveResult, ProposedFact, TaskData

log = setup_logging("web-ui")

ORCHESTRATOR_URL = settings.orchestrator_url
BRANCH_NAME_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")

app = FastAPI(title="Auto-Agent Chat")

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Connected websocket clients: ws -> {"user_id": int, "username": str}
connected_clients: dict[WebSocket, dict] = {}

# ─── Memory tab upload endpoint ──────────────────────────────────

MEMORY_MAX_CHARS = 200_000


@dataclass
class MemorySession:
    text: str
    user_id: int = 0
    created_at: float = 0.0
    char_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.char_count = len(self.text)
        if self.created_at == 0.0:
            self.created_at = _time.time()


# keyed by source_id; cleared on save-success, ws disconnect, or TTL sweep
memory_sessions: dict[str, MemorySession] = {}

MEMORY_SESSION_TTL_SEC = 30 * 60  # 30 minutes


async def _require_user(token: str | None = Query(default=None)) -> int:
    """Accept token as ?token=... query param."""
    from orchestrator.auth import verify_token
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="invalid token")
    return int(payload["user_id"])


@app.post("/memory/upload")
@app.post("/api/memory/upload")
async def memory_upload(
    file: UploadFile = File(...),
    user_id: int = Depends(_require_user),
) -> dict:
    """Parse an uploaded file to text, hold the text on the server, discard bytes."""
    name = (file.filename or "").lower()
    if name.endswith(".pdf"):
        raw = await file.read()
        try:
            from pypdf import PdfReader
            reader = PdfReader(_BytesIO(raw))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"could not parse pdf: {e}") from e
        finally:
            del raw
    elif name.endswith((".txt", ".md", ".log")):
        raw = await file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(status_code=400, detail=f"not utf-8: {e}") from e
        finally:
            del raw
    else:
        raise HTTPException(status_code=400, detail="only .txt, .md, .log, .pdf are supported")

    if len(text) > MEMORY_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"file too large: {len(text)} chars (cap {MEMORY_MAX_CHARS})",
        )

    source_id = f"src-{_uuid.uuid4().hex[:12]}"
    memory_sessions[source_id] = MemorySession(text=text, user_id=user_id)
    return {"source_id": source_id, "char_count": len(text)}


async def broadcast(message: dict) -> None:
    """Send a message to all connected websocket clients."""
    dead: set[WebSocket] = set()
    for ws in connected_clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    for ws in dead:
        connected_clients.pop(ws, None)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    # Authenticate via cookie (preferred) or token query param (legacy fallback)
    token = ws.cookies.get("auto_agent_session") or ws.query_params.get("token")
    if not token:
        await ws.send_json({"type": "error", "message": "Authentication required"})
        await ws.close(code=4001)
        return

    from orchestrator.auth import verify_token
    payload = verify_token(token)
    if not payload:
        await ws.send_json({"type": "error", "message": "Invalid or expired token"})
        await ws.close(code=4001)
        return

    user_id = payload["user_id"]
    username = payload["username"]
    connected_clients[ws] = {"user_id": user_id, "username": username}

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
                await _handle_create_task(ws, data, user_id)
            elif msg_type == "send_message":
                await _handle_send_message(ws, data, user_id, username)
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
                        hist_resp, msg_resp = await asyncio.gather(
                            client.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}/history"),
                            client.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}/messages"),
                        )
                        entries = hist_resp.json() if hist_resp.status_code == 200 else []
                        messages = msg_resp.json() if msg_resp.status_code == 200 else []
                        await ws.send_json(
                            {
                                "type": "history",
                                "task_id": task_id,
                                "entries": entries,
                                "messages": messages,
                            }
                        )
            elif msg_type == "send_guidance":
                # Pair-programming: user sends guidance to a running agent.
                # Persist via the HTTP API (which also pushes to Redis for
                # real-time delivery) so the message survives UI reloads.
                task_id = data.get("task_id")
                message = data.get("message", "").strip()
                if task_id and message:
                    headers = {"X-Sender": username} if username else {}
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            f"{ORCHESTRATOR_URL}/tasks/{task_id}/messages",
                            json={"content": message},
                            headers=headers,
                        )
                    # Echo back to all clients so they see their own message
                    await broadcast({
                        "type": "guidance_sent",
                        "task_id": task_id,
                        "message": message,
                        "username": username,
                    })
            elif msg_type == "refresh":
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{ORCHESTRATOR_URL}/tasks")
                    if resp.status_code == 200:
                        tasks = [TaskData.model_validate(t).model_dump() for t in resp.json()]
                        await ws.send_json({"type": "task_list", "tasks": tasks})
            elif msg_type == "memory_extract":
                await _handle_memory_extract(ws, data, user_id)
            elif msg_type == "memory_reextract":
                await _handle_memory_reextract(ws, data, user_id)
            elif msg_type == "memory_save":
                await _handle_memory_save(ws, data, user_id)

    except WebSocketDisconnect:
        connected_clients.pop(ws, None)
        # Clean up any memory sessions owned by this disconnecting user
        stale = [sid for sid, s in memory_sessions.items() if s.user_id == user_id]
        for sid in stale:
            memory_sessions.pop(sid, None)


async def _handle_create_task(ws: WebSocket, data: dict, user_id: int | None = None) -> None:
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

    task_payload: dict = {
        "title": title,
        "description": description,
        "source": "manual",
        "repo_name": repo_name,
    }
    if user_id is not None:
        task_payload["created_by_user_id"] = user_id

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/tasks",
            json=task_payload,
        )
        if resp.status_code == 200:
            task = TaskData.model_validate(resp.json())
            await broadcast({"type": "system", "message": f"Task #{task.id} created: {task.title}"})
        else:
            await ws.send_json({"type": "error", "message": f"Failed to create task: {resp.text}"})


async def _handle_send_message(
    ws: WebSocket, data: dict, user_id: int | None = None, username: str = ""
) -> None:
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
        auto_payload: dict = {
            "title": text[:120],
            "description": text,
            "source": "manual",
        }
        if user_id is not None:
            auto_payload["created_by_user_id"] = user_id
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/tasks",
                json=auto_payload,
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

    # Persist the message in task history so it survives page refresh
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/message",
            json={"message": text, "username": username or "unknown"},
        )

    r = await get_redis()
    event = Event(
        type="human.message",
        task_id=task_id,
        payload={"message": text, "source": "web"},
    )
    await publish_event(r, event.to_redis())
    await r.aclose()

    await broadcast({"type": "user", "message": text, "task_id": task_id, "username": username})


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


# --- Memory tab websocket handlers ---


async def _send_memory_error(ws, message: str) -> None:
    await ws.send_json({"type": "memory_error", "message": message})


async def _run_memory_extract(
    ws, text: str, hint: str | None, source_id: str | None,
) -> None:
    """Shared core for extract + reextract."""
    if len(text) > MEMORY_MAX_CHARS:
        await _send_memory_error(ws, f"input too large: {len(text)} chars (cap {MEMORY_MAX_CHARS})")
        return

    # First pass: extract with NO existing-facts context (we need entity names first).
    try:
        first_pass = await extract(text=text, hint=hint, existing_facts_by_entity={})
    except ValueError as e:
        await _send_memory_error(ws, f"extraction failed: {e}")
        return

    # Look up existing facts for each proposed entity.
    existing_by_entity: dict[str, list[dict]] = {}
    entity_match: dict[str, dict] = {}
    for row in first_pass:
        if row.entity in existing_by_entity:
            continue
        match = await recall_entity(row.entity)
        if match:
            entity_match[row.entity] = match
            existing_by_entity[row.entity] = match.get("facts", [])

    # Second pass if we found existing entities — lets the LLM tag conflicts.
    if existing_by_entity:
        try:
            rows = await extract(text=text, hint=hint, existing_facts_by_entity=existing_by_entity)
        except ValueError as e:
            await _send_memory_error(ws, f"extraction failed: {e}")
            return
    else:
        rows = first_pass

    # Annotate each row with entity_status + score.
    for row in rows:
        if row.entity in entity_match:
            row.entity_status = "exists"
            row.entity_match_score = entity_match[row.entity].get("score")
        else:
            row.entity_status = "new"

    await ws.send_json({
        "type": "memory_rows",
        "source_id": source_id,
        "rows": [r.model_dump() for r in rows],
    })


async def _handle_memory_extract(ws, data: dict, user_id: int = 0) -> None:
    source_id = data.get("source_id")
    pasted = data.get("pasted_text")
    hint = (data.get("context_hint") or "").strip() or None

    if bool(source_id) == bool(pasted):
        await _send_memory_error(ws, "provide exactly one of source_id or pasted_text")
        return

    if source_id:
        sess = memory_sessions.get(source_id)
        if not sess:
            await _send_memory_error(ws, f"unknown source_id: {source_id}")
            return
        if sess.user_id != user_id:
            await _send_memory_error(ws, "access denied: session belongs to another user")
            return
        text = sess.text
    else:
        text = pasted

    await _run_memory_extract(ws, text=text, hint=hint, source_id=source_id)


async def _handle_memory_reextract(ws, data: dict, user_id: int = 0) -> None:
    source_id = data.get("source_id")
    note = (data.get("note") or "").strip()
    sess = memory_sessions.get(source_id) if source_id else None
    if not sess:
        await _send_memory_error(ws, "no source in session; re-upload or re-paste")
        return
    if sess.user_id != user_id:
        await _send_memory_error(ws, "access denied: session belongs to another user")
        return
    hint = f"User correction note: {note}" if note else None
    await _run_memory_extract(ws, text=sess.text, hint=hint, source_id=source_id)


async def _handle_memory_save(ws, data: dict, user_id: int = 0) -> None:
    rows_raw = data.get("rows") or []
    rows: list[ProposedFact] = []
    for r in rows_raw:
        try:
            rows.append(ProposedFact.model_validate(r))
        except Exception as e:
            await _send_memory_error(ws, f"invalid row: {e}")
            return

    # Guard: every conflict row needs a resolution.
    for row in rows:
        if row.conflicts and row.resolution is None:
            await _send_memory_error(
                ws,
                f"row {row.row_id} has a conflict but no resolution chosen",
            )
            return

    results: list[MemorySaveResult] = []
    source_id = data.get("source_id")
    if source_id:
        sess = memory_sessions.get(source_id)
        if sess and sess.user_id != user_id:
            await _send_memory_error(ws, "access denied: session belongs to another user")
            return
    for row in rows:
        try:
            if row.conflicts and row.resolution == "keep_existing":
                results.append(MemorySaveResult(row_id=row.row_id, ok=True))
                continue
            if row.conflicts and row.resolution == "replace":
                fact_id = None
                for c in row.conflicts:
                    fact_id = await correct_fact(c.fact_id, row.content)
                results.append(MemorySaveResult(row_id=row.row_id, ok=True, fact_id=fact_id))
                continue
            fid = await remember_row(row)
            results.append(MemorySaveResult(row_id=row.row_id, ok=True, fact_id=fid))
        except Exception as e:
            results.append(MemorySaveResult(row_id=row.row_id, ok=False, error=str(e)))

    if source_id and all(r.ok for r in results):
        memory_sessions.pop(source_id, None)

    await ws.send_json({
        "type": "memory_saved",
        "results": [r.model_dump() for r in results],
    })


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


async def agent_stream_listener() -> None:
    """Subscribe to agent live-stream channels via Redis pub/sub.

    The agent publishes tool calls and thinking to `task:{id}:stream`.
    We forward these to WebSocket clients so the UI shows real-time
    agent activity — the pair-programming feed.
    """
    import json as _json
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.psubscribe("task:*:stream")
    log.info("Agent stream listener started (pub/sub)")

    while True:
        try:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg["type"] == "pmessage":
                channel = msg["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                # Extract task_id from channel name "task:123:stream"
                parts = channel.split(":")
                task_id = int(parts[1]) if len(parts) >= 3 else 0

                data = msg["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                payload = _json.loads(data)

                await broadcast({
                    "type": "agent_stream",
                    "task_id": task_id,
                    **payload,
                })
            else:
                await asyncio.sleep(0.05)  # Small sleep when no messages
        except Exception:
            log.exception("Agent stream listener error")
            await asyncio.sleep(2)
            try:
                r = await get_redis()
                pubsub = r.pubsub()
                await pubsub.psubscribe("task:*:stream")
            except Exception:
                pass


async def _memory_sessions_sweeper() -> None:
    """Background task: remove memory sessions older than MEMORY_SESSION_TTL_SEC."""
    while True:
        await asyncio.sleep(60)
        _sweep_memory_sessions_once()


def _sweep_memory_sessions_once() -> None:
    """Remove stale memory sessions (extracted so tests can call it directly)."""
    now = _time.time()
    stale = [sid for sid, s in memory_sessions.items() if now - s.created_at > MEMORY_SESSION_TTL_SEC]
    for sid in stale:
        memory_sessions.pop(sid, None)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(event_listener())
    asyncio.create_task(agent_stream_listener())
    asyncio.create_task(_memory_sessions_sweeper())


if __name__ == "__main__":
    uvicorn.run("web.main:app", host="0.0.0.0", port=2020, reload=True)
