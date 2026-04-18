"""Tool registry and factory."""

from __future__ import annotations

from agent.tools.base import Tool, ToolContext, ToolRegistry
from agent.tools.bash import BashTool
from agent.tools.file_edit import FileEditTool
from agent.tools.file_read import FileReadTool
from agent.tools.file_write import FileWriteTool
from agent.tools.git import GitTool
from agent.tools.glob_tool import GlobTool
from agent.tools.grep_tool import GrepTool
from agent.tools.memory_read import MemoryReadTool
from agent.tools.memory_write import MemoryWriteTool
from agent.tools.skill import SkillTool
from agent.tools.subagent import SubagentTool
from agent.tools.test_runner import TestRunnerTool


def create_default_registry(readonly: bool = False) -> ToolRegistry:
    """Create a registry with all standard coding tools.

    Args:
        readonly: If True, exclude tools that modify files (planning mode).
    """
    registry = ToolRegistry()

    # Always available — read-only tools
    registry.register(FileReadTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(GitTool())
    registry.register(SkillTool())  # Load superpowers methodology
    registry.register(MemoryReadTool())  # Graph memory search/traverse

    # Write tools — excluded in planning/readonly mode
    if not readonly:
        registry.register(FileWriteTool())
        registry.register(FileEditTool())
        registry.register(BashTool())
        registry.register(TestRunnerTool())
        registry.register(SubagentTool())  # Dispatch parallel workers
        registry.register(MemoryWriteTool())  # Graph memory mutations

    return registry
