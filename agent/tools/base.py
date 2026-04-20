"""Abstract tool interface and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agent.llm.types import ToolDefinition


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
