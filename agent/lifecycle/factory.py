"""Agent factory + UI streaming hooks.

Builds an ``AgentLoop`` configured for a particular phase, with optional
heartbeat / streaming / guidance callbacks wired through the
``TaskChannel`` seam when a ``task_id`` is given. Three external workers
(``po_analyzer``, ``harness``, ``architect_analyzer``) consume
``create_agent`` directly — it's already public-by-use, so the leading
underscore is dropped.
"""

from __future__ import annotations

from agent.context import ContextManager
from agent.llm import get_provider
from agent.loop import AgentLoop
from agent.session import Session
from agent.tools import create_default_registry
from shared.task_channel import task_channel


async def home_dir_for_task(task) -> str | None:
    """Return the effective vault HOME for a task.

    Resolution order:
      1. If the owner has paired their own Claude credentials → owner's vault.
      2. Otherwise, if a fallback user is configured → fallback's vault.
      3. Otherwise → None (caller falls back to legacy container HOME, used by
         system-driven flows like repo summary).
    """
    from orchestrator.claude_auth import resolve_home_dir

    user_id = getattr(task, "created_by_user_id", None)
    return await resolve_home_dir(user_id)


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


def create_agent(
    workspace: str,
    session_id: str | None = None,
    readonly: bool = False,
    with_web: bool = False,
    with_browser: bool = False,
    with_consult_architect: bool = False,
    max_turns: int = 50,
    include_methodology: bool = False,
    model_tier: str | None = None,
    task_id: int | None = None,
    task_description: str | None = None,
    repo_name: str | None = None,
    complexity: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
    dev_server_log_path: str | None = None,
) -> AgentLoop:
    """Create a configured AgentLoop instance.

    Args:
        model_tier: Override model selection. Use "fast" for mechanical tasks,
                   "standard" for normal work, "capable" for complex architecture.
        task_id: If set, the agent sends heartbeat signals via the
                ``TaskChannel`` seam so the timeout watchdog knows it's
                making progress, and streams tool calls / thinking to
                the UI.
        org_id: If set (together with task_id), a UsageSink is constructed
               and attached to the loop so every LLM call is accounted
               against the org's daily token quota.
        with_web: If True, include web_search + fetch_url tools (researcher mode).
        with_browser: If True, include browse_url + tail_dev_server_log (verify mode).
        with_consult_architect: If True, expose ``consult_architect`` to the
            agent — only set this for trio child (builder) tasks; the tool
            refuses to run when ``parent_task_id`` is absent from the
            ToolContext, but gating registration too keeps it out of the
            tool catalogue for non-trio agents.
    """
    from agent.loop import UsageSink

    provider = get_provider(model_override=model_tier, home_dir=home_dir)
    tools = create_default_registry(
        readonly=readonly,
        with_web=with_web,
        with_browser=with_browser,
        with_consult_architect=with_consult_architect,
    )
    ctx = ContextManager(workspace, provider)
    session = Session(session_id) if session_id else None

    usage_sink = (
        UsageSink(org_id=org_id, task_id=task_id) if org_id is not None else None
    )

    heartbeat = None
    on_tool_call = None
    on_thinking = None
    get_guidance = None

    if task_id:
        channel = task_channel(task_id)

        async def heartbeat():
            await channel.heartbeat()

        async def on_tool_call(tool_name: str, args: dict, result_preview: str, turn: int):
            """Stream tool calls to the UI via the TaskChannel seam."""
            await channel.stream_tool_call(
                tool=tool_name,
                args_preview=_format_tool_args(tool_name, args),
                result_preview=result_preview[:150],
                turn=turn,
            )

        async def on_thinking(text: str, turn: int):
            """Stream assistant thinking/reasoning to the UI."""
            if len(text) > 20:  # Skip trivial empty responses
                await channel.stream_thinking(text=text[:500], turn=turn)

        async def get_guidance() -> str | None:
            """Check for user guidance messages sent via the UI."""
            return await channel.pop_guidance()

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
        home_dir=home_dir,
        usage_sink=usage_sink,
        dev_server_log_path=dev_server_log_path,
    )
