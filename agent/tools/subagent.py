"""Subagent tool — dispatch independent workers for parallel tasks.

Enables the superpowers subagent-driven-development pattern: the main
agent breaks work into independent tasks and dispatches each to a fresh
subagent with its own context. Results are collected back.

Each subagent:
- Gets a fresh AgentLoop with its own conversation (context isolation)
- Shares the same workspace (can see files the main agent wrote)
- Has access to all tools except subagent (no recursive spawning)
- Runs up to max_turns with a timeout
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from agent.tools.base import Tool, ToolContext, ToolResult

logger = structlog.get_logger()


class SubagentTool(Tool):
    name = "subagent"
    description = (
        "Dispatch an independent subagent to work on a specific subtask. "
        "The subagent gets a fresh conversation context but shares the workspace "
        "(can see and modify files). Use this for:\n"
        "- Parallel implementation of independent components\n"
        "- Code review by a fresh-eyes reviewer\n"
        "- Research tasks that don't need the main conversation history\n\n"
        "The subagent runs to completion and returns its output. "
        "Do NOT dispatch subagents for tasks that depend on each other — "
        "do those sequentially yourself."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Complete task description for the subagent. Include all context "
                    "it needs — it cannot see your conversation history. Be specific: "
                    "file paths, expected behavior, constraints."
                ),
            },
            "max_turns": {
                "type": "integer",
                "description": "Maximum turns for the subagent (default 15).",
                "default": 15,
            },
        },
        "required": ["task"],
    }
    is_readonly = False  # Subagents can write files

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.readonly:
            return ToolResult(output="Subagent not available in readonly mode.", is_error=True)

        task = arguments.get("task", "")
        max_turns = min(arguments.get("max_turns", 15), 30)  # Cap at 30

        if not task.strip():
            return ToolResult(output="Error: task description is empty.", is_error=True)

        logger.info("subagent_dispatched", task_preview=task[:100], max_turns=max_turns)

        try:
            # Import here to avoid circular imports
            from agent.context import ContextManager
            from agent.llm import get_provider
            from agent.tools import create_default_registry

            provider = get_provider()
            # Give the subagent all tools EXCEPT subagent (no recursive spawning)
            tools = create_default_registry(readonly=False)
            if tools.get("subagent"):
                tools._tools.pop("subagent", None)

            ctx = ContextManager(context.workspace, provider)
            from agent.loop import AgentLoop
            subagent = AgentLoop(
                provider=provider,
                tools=tools,
                context_manager=ctx,
                max_turns=max_turns,
                workspace=context.workspace,
            )

            result = await asyncio.wait_for(
                subagent.run(task),
                timeout=600,  # 10 minute hard timeout per subagent
            )

            # Close the provider's client
            if hasattr(provider, '_client'):
                try:
                    if hasattr(provider._client, '_client') and hasattr(provider._client._client, 'aclose'):
                        await provider._client._client.aclose()
                except Exception:
                    pass

            output = result.output or "(subagent produced no output)"
            summary = (
                f"Subagent completed in {result.tool_calls_made} tool calls.\n\n"
                f"Output:\n{output}"
            )

            logger.info(
                "subagent_completed",
                tool_calls=result.tool_calls_made,
                output_len=len(output),
            )

            return ToolResult(
                output=summary,
                token_estimate=len(summary) // 4,
            )

        except asyncio.TimeoutError:
            logger.warning("subagent_timeout", task_preview=task[:100])
            return ToolResult(
                output="Subagent timed out after 10 minutes.",
                is_error=True,
            )
        except Exception as e:
            logger.error("subagent_error", error=str(e))
            return ToolResult(
                output=f"Subagent failed: {e}",
                is_error=True,
            )
