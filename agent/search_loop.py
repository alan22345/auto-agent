"""Run one search-tab agent turn and yield NDJSON event dicts.

This is a thin shell around `agent.loop.AgentLoop`:

  * Builds a curated ToolRegistry: web_search, fetch_url, recall_memory,
    remember_memory.
  * Builds a search-specific system prompt that tells the agent to recall
    team-memory before searching the web, and to use remember_memory only
    for user-stated preferences (not research).
  * Translates the loop's on_tool_call / on_thinking callbacks plus tools'
    own event_sink emissions into NDJSON event dicts on a queue.
  * Yields events to the HTTP layer until the loop completes or errors.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from agent.context import ContextManager
from agent.context.memory import query_relevant_memory
from agent.llm import get_provider
from agent.llm.types import Message
from agent.loop import AgentLoop
from agent.tools.base import ToolRegistry
from agent.tools.fetch_url import FetchUrlTool
from agent.tools.recall_memory import RecallMemoryTool
from agent.tools.remember_memory import RememberMemoryTool
from agent.tools.web_search import WebSearchTool

logger = structlog.get_logger()

_SYSTEM_PROMPT = """You are a research assistant in the Auto-Agent search tab.

You have four tools:
  - recall_memory: look up the team-memory knowledge graph
  - web_search: Brave search for current web information
  - fetch_url: read the full text of a specific URL
  - remember_memory: save a fact to team-memory

Workflow for each user message:
1. If the question is about the user, the team, or this project, START with
   recall_memory. The answer may already be there.
2. If the question is about the wider world or current events, use web_search.
   Use multiple targeted queries rather than one broad query.
3. If a result's snippet looks promising but lacks detail, use fetch_url.
4. Synthesize a markdown answer with concise inline citations like
   [example.com](https://example.com).
5. Use remember_memory ONLY when the user has explicitly asked you to
   remember something, or has stated a durable preference about themselves
   or the project. Do NOT use it to save web research.

Be terse. Use bullet points and short paragraphs."""


def _build_tools(brave_api_key: str, author: str | None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(WebSearchTool(api_key=brave_api_key))
    registry.register(FetchUrlTool())
    registry.register(RecallMemoryTool())
    registry.register(RememberMemoryTool(author=author))
    return registry


def _history_to_messages(history: list[dict]) -> list[Message]:
    out: list[Message] = []
    for h in history:
        role = h.get("role")
        content = h.get("content") or ""
        if role in ("user", "assistant") and content:
            out.append(Message(role=role, content=content))
    return out


async def run_search_turn(
    *,
    user_message: str,
    history: list[dict],
    brave_api_key: str,
    author: str | None,
) -> AsyncIterator[dict]:
    """Run one search agent turn. Yields NDJSON event dicts.

    Event types emitted:
      * tool_call_start  {tool, args}
      * source           {url, title, summary, query}
      * memory_hit       {entity, facts}
      * text             {delta}
      * done             {answer}
      * error            {message}
    """

    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def event_sink(event: dict) -> None:
        await queue.put(event)

    async def on_tool_call(name: str, args: dict, _preview: str, _turn: int) -> None:
        await queue.put({"type": "tool_call_start", "tool": name, "args": args})

    async def on_thinking(text: str, _turn: int) -> None:
        if text:
            await queue.put({"type": "text", "delta": text})

    tools = _build_tools(brave_api_key, author)

    pre_recall = await query_relevant_memory(user_message)
    system_prompt = _SYSTEM_PROMPT + (("\n\n" + pre_recall) if pre_recall else "")

    # Search always runs on Bedrock so the agentic tool-calling loop streams
    # source/text/tool_call_start events to the UI and reports a real token
    # count per turn. The claude_cli passthrough provider can't do either.
    provider = get_provider(provider_override="bedrock")
    context_manager = ContextManager(provider=provider, workspace=".")

    prior_messages = _history_to_messages(history)

    loop = AgentLoop(
        provider=provider,
        tools=tools,
        context_manager=context_manager,
        session=None,
        max_turns=12,
        workspace=".",
        on_tool_call=on_tool_call,
        on_thinking=on_thinking,
        event_sink=event_sink,
    )

    async def runner() -> None:
        try:
            prior_summary = "\n\n".join(
                f"{m.role.upper()}: {m.content}" for m in prior_messages
            )
            full_prompt = (
                (f"Previous turns in this session:\n{prior_summary}\n\n---\n\n"
                 if prior_summary else "")
                + f"User: {user_message}"
            )
            result = await loop.run(prompt=full_prompt, system=system_prompt)
            await queue.put({
                "type": "done",
                "answer": result.output,
                "input_tokens": result.tokens_used.input_tokens,
                "output_tokens": result.tokens_used.output_tokens,
            })
        except Exception as e:
            logger.warning("search_loop_failed", error=str(e))
            await queue.put({"type": "error", "message": str(e)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(runner())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
