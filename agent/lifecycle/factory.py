"""Agent factory + UI streaming hooks.

Builds an ``AgentLoop`` configured for a particular phase, with optional
heartbeat / streaming / guidance callbacks wired through Redis when a
``task_id`` is given. Three external workers (``po_analyzer``, ``harness``,
``architect_analyzer``) consume ``create_agent`` directly — it's already
public-by-use, so the leading underscore is dropped.
"""

from __future__ import annotations

from agent.context import ContextManager
from agent.llm import get_provider
from agent.loop import AgentLoop
from agent.session import Session
from agent.tools import create_default_registry
from shared.redis_client import get_redis


def _format_tool_args(tool_name: str, args: dict) -> str:
    """Format tool args into a human-readable preview for the streaming UI."""
    if tool_name == "file_read" or tool_name == "file_write" or tool_name == "file_edit":
        return args.get("file_path", "?")
    elif tool_name == "grep":
        path = args.get("path", "")
        return f'"{args.get("pattern", "?")}"' + (f" in {path}" if path else "")
    elif tool_name == "glob":
        return args.get("pattern", "?")
    elif tool_name == "bash":
        return args.get("command", "?")[:100]
    elif tool_name == "git":
        return args.get("command", "?")[:80]
    elif tool_name == "test_runner":
        return args.get("target", "") or "full suite"
    return str(args)[:100]


async def _stream_to_task(task_id: int, event_type: str, payload: dict) -> None:
    """Publish a live-stream event for a task. The web UI picks these up
    via WebSocket and renders them in the task's chat feed."""
    try:
        r = await get_redis()
        import json

        await r.publish(
            f"task:{task_id}:stream",
            json.dumps({"type": event_type, **payload}),
        )
        await r.aclose()
    except Exception:
        pass  # Best-effort — don't break the agent if streaming fails


async def _check_guidance(task_id: int) -> str | None:
    """Check for a user guidance message sent via the UI.

    The web UI pushes guidance to a Redis list. We LPOP one message per
    check (one per turn). Returns None if no guidance is pending.
    """
    try:
        r = await get_redis()
        msg = await r.lpop(f"task:{task_id}:guidance")
        await r.aclose()
        if msg:
            return msg.decode() if isinstance(msg, bytes) else str(msg)
    except Exception:
        pass
    return None


async def _heartbeat_for_task(task_id: int) -> None:
    """Update a Redis key to signal the agent is alive and making progress.

    The timeout watchdog checks this key. If it exists, the task is alive
    regardless of how long ago `updated_at` was set. TTL=15 minutes.
    """
    try:
        r = await get_redis()
        await r.set(f"task:{task_id}:heartbeat", "1", ex=900)  # 15-min TTL
        await r.aclose()
    except Exception:
        pass  # Best-effort


def create_agent(
    workspace: str,
    session_id: str | None = None,
    readonly: bool = False,
    max_turns: int = 50,
    include_methodology: bool = False,
    model_tier: str | None = None,
    task_id: int | None = None,
    task_description: str | None = None,
    repo_name: str | None = None,
    complexity: str | None = None,
) -> AgentLoop:
    """Create a configured AgentLoop instance.

    Args:
        model_tier: Override model selection. Use "fast" for mechanical tasks,
                   "standard" for normal work, "capable" for complex architecture.
        task_id: If set, the agent sends heartbeat signals via Redis so the
                timeout watchdog knows it's making progress.
    """
    provider = get_provider(model_override=model_tier)
    tools = create_default_registry(readonly=readonly)
    ctx = ContextManager(workspace, provider)
    session = Session(session_id) if session_id else None

    heartbeat = None
    on_tool_call = None
    on_thinking = None
    get_guidance = None

    if task_id:

        async def heartbeat():
            await _heartbeat_for_task(task_id)

        async def on_tool_call(tool_name: str, args: dict, result_preview: str, turn: int):
            """Stream tool calls to the UI via Redis → WebSocket."""
            await _stream_to_task(
                task_id,
                "tool",
                {
                    "tool": tool_name,
                    "args_preview": _format_tool_args(tool_name, args),
                    "result_preview": result_preview[:150],
                    "turn": turn,
                },
            )

        async def on_thinking(text: str, turn: int):
            """Stream assistant thinking/reasoning to the UI."""
            if len(text) > 20:  # Skip trivial empty responses
                await _stream_to_task(
                    task_id,
                    "thinking",
                    {"text": text[:500], "turn": turn},
                )

        async def get_guidance() -> str | None:
            """Check for user guidance messages sent via the UI."""
            return await _check_guidance(task_id)

    return AgentLoop(
        provider=provider,
        tools=tools,
        context_manager=ctx,
        session=session,
        max_turns=max_turns,
        workspace=workspace,
        include_methodology=include_methodology,
        task_description=task_description,
        heartbeat=heartbeat,
        on_tool_call=on_tool_call,
        on_thinking=on_thinking,
        get_guidance=get_guidance,
        repo_name=repo_name,
        complexity=complexity,
    )
