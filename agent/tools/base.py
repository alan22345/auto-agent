"""Abstract tool interface and registry."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent.llm.types import ToolDefinition

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass
class ToolResult:
    """Result returned by a tool execution."""

    output: str
    token_estimate: int = 0
    is_error: bool = False


@dataclass
class ToolContext:
    """Execution context passed to every tool."""

    workspace: str  # Absolute path to the repo root
    readonly: bool = False  # Planning mode — block writes
    # Optional async sink for tools that emit progress events to a streaming
    # caller (e.g. web_search emits 'source' events as Brave results arrive).
    event_sink: Callable[[dict], Awaitable[None]] | None = None
    # Carried from the parent AgentLoop so nested subagents can inherit quota
    # accounting without re-constructing a UsageSink from scratch.
    usage_sink: object | None = None  # UsageSink | None — typed as object to avoid circular import
    # Path to dev-server log file when a dev server is running in this phase.
    # Used by TailDevServerLogTool; None when no server is active.
    dev_server_log_path: str | None = None

    def resolve(self, path: str) -> str | None:
        """Resolve a path against the workspace, refusing escapes.

        Accepts a relative path (joined under ``workspace``) or an absolute path
        (must already be inside the workspace). Returns the canonical absolute
        real path on success, or ``None`` if the path would escape the sandbox.

        This is the single seam every path-touching tool routes through, so the
        sandboxing invariant lives in one place — including the trailing-os.sep
        guard that prevents a workspace named ``/work`` from accepting paths
        like ``/workshop/secret``.
        """
        if os.path.isabs(path):
            resolved = os.path.realpath(path)
        else:
            resolved = os.path.realpath(os.path.join(self.workspace, path))
        ws_real = os.path.realpath(self.workspace)
        if resolved == ws_real or resolved.startswith(ws_real + os.sep):
            return resolved
        return None


class Tool(ABC):
    """Base class for all agent tools."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments
    is_readonly: bool = True  # Override to False for write/edit/bash tools

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool with the given arguments."""

    def to_definition(self) -> ToolDefinition:
        """Convert to the schema sent to the LLM."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


class ToolRegistry:
    """Holds available tools and converts them to LLM-ready definitions."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        """Return all tool definitions for the LLM API call."""
        return [t.to_definition() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())
