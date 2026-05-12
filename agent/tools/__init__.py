"""Tool registry and factory."""

from __future__ import annotations

from agent.tools.base import Tool, ToolContext, ToolRegistry
from agent.tools.bash import BashTool
from agent.tools.fetch_url import FetchUrlTool
from agent.tools.file_edit import FileEditTool
from agent.tools.file_read import FileReadTool
from agent.tools.file_write import FileWriteTool
from agent.tools.git import GitTool
from agent.tools.glob_tool import GlobTool
from agent.tools.grep_tool import GrepTool
from agent.tools.recall_memory import RecallMemoryTool
from agent.tools.remember_memory import RememberMemoryTool
from agent.tools.skill import SkillTool
from agent.tools.subagent import SubagentTool
from agent.tools.test_runner import TestRunnerTool
from agent.tools.web_search import WebSearchTool


def create_default_registry(
    readonly: bool = False, with_web: bool = False
) -> ToolRegistry:
    """Create a registry with all standard coding tools.

    Args:
        readonly: If True, exclude tools that modify files (planning mode).
        with_web: If True, include web_search + fetch_url (researcher mode).
    """
    registry = ToolRegistry()

    # Always available — read-only tools
    registry.register(FileReadTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(GitTool())
    registry.register(SkillTool())  # Load superpowers methodology
    registry.register(RecallMemoryTool())  # Query shared team-memory graph

    if with_web:
        from shared.config import settings

        registry.register(WebSearchTool(api_key=settings.brave_api_key))
        registry.register(FetchUrlTool())

    # Write tools — excluded in planning/readonly mode
    if not readonly:
        registry.register(FileWriteTool())
        registry.register(FileEditTool())
        registry.register(BashTool())
        registry.register(TestRunnerTool())
        registry.register(SubagentTool())  # Dispatch parallel workers
        registry.register(RememberMemoryTool())  # Persist team-memory facts

    return registry
