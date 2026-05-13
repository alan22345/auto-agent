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
    readonly: bool = False,
    with_web: bool = False,
    with_browser: bool = False,
    with_consult_architect: bool = False,
    with_architect_tools: bool = False,
) -> ToolRegistry:
    """Create a registry with all standard coding tools.

    Args:
        readonly: If True, exclude tools that modify files (planning mode).
        with_web: If True, include web_search + fetch_url (researcher mode).
        with_browser: If True, include browse_url + tail_dev_server_log (verify mode).
        with_consult_architect: Add consult_architect (builder-side, trio children only).
        with_architect_tools: Add record_decision + request_market_brief (architect agent only).
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

    if with_browser:
        from agent.tools.browse_url import BrowseUrlTool
        from agent.tools.dev_server import TailDevServerLogTool

        registry.register(BrowseUrlTool())
        registry.register(TailDevServerLogTool())

    if with_consult_architect:
        from agent.tools.consult_architect import ConsultArchitectTool

        registry.register(ConsultArchitectTool())

    if with_architect_tools:
        from agent.tools.record_decision import RecordDecisionTool
        from agent.tools.request_market_brief import RequestMarketBriefTool

        registry.register(RecordDecisionTool())
        registry.register(RequestMarketBriefTool())

    # Write tools — excluded in planning/readonly mode
    if not readonly:
        registry.register(FileWriteTool())
        registry.register(FileEditTool())
        registry.register(BashTool())
        registry.register(TestRunnerTool())
        registry.register(SubagentTool())  # Dispatch parallel workers
        registry.register(RememberMemoryTool())  # Persist team-memory facts

    return registry
