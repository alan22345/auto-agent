"""Agent tools for reading per-repo project secrets (ADR-019 §5).

Two tools:
  - list_repo_secrets: returns key names + presence/source/purpose, never values.
  - get_secret: returns a single plaintext value (registered with the structlog
    redactor on read so it is scrubbed from log output).

Both tools require ToolContext.repo_id and ToolContext.organization_id to be
set.  When running outside a repo workspace (PO analyzer, harness onboarding,
etc.) the context fields are None and the tools return an error result.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar  # used by execute() signatures

from agent.tools.base import Tool, ToolContext, ToolResult

# Keys must be uppercase env-var-style names (same regex as shared/repo_secrets.py).
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_NO_WORKSPACE_MSG = (
    "This tool is only available when the agent is operating inside a repo workspace."
)


class ListRepoSecretsTool(Tool):
    """List the keys of secrets configured for this repo's project."""

    name = "list_repo_secrets"
    description = (
        "List the keys of secrets configured for this repo's project "
        "(e.g. STRIPE_API_KEY). Returns names + whether each is set + source + purpose. "
        "Never returns values."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.repo_id is None or context.organization_id is None:
            return ToolResult(output=_NO_WORKSPACE_MSG, is_error=True)

        from shared import repo_secrets

        rows = await repo_secrets.list_keys(
            context.repo_id,
            organization_id=context.organization_id,
        )

        if not rows:
            return ToolResult(output="No secrets configured for this repo.", token_estimate=5)

        # Render as a human-readable table (same style as other list tools in
        # this codebase) so the LLM can scan it easily.
        lines = ["KEY | SET | SOURCE | PURPOSE"]
        lines.append("-" * 60)
        for row in rows:
            key = row.get("key", "")
            is_set = "yes" if row.get("set") else "no"
            source = row.get("source", "")
            purpose = row.get("purpose") or ""
            lines.append(f"{key} | {is_set} | {source} | {purpose}")

        output = "\n".join(lines)
        return ToolResult(output=output, token_estimate=len(output) // 3)


class GetSecretTool(Tool):
    """Get the plaintext value of a single secret for this repo."""

    name = "get_secret"
    description = (
        "Get the plaintext value of a single secret for this repo. "
        "Returns null if the key isn't set. "
        "Note: the value enters this conversation's context and will appear in API logs."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Secret key name, e.g. STRIPE_API_KEY. "
                    "Must match ^[A-Z][A-Z0-9_]*$ (uppercase letters, digits, underscores)."
                ),
                "pattern": "^[A-Z][A-Z0-9_]*$",
            }
        },
        "required": ["key"],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.repo_id is None or context.organization_id is None:
            return ToolResult(output=_NO_WORKSPACE_MSG, is_error=True)

        key = arguments.get("key", "")
        if not _KEY_RE.match(key):
            return ToolResult(
                output=(
                    f"Invalid key format: {key!r}. "
                    "Key must match ^[A-Z][A-Z0-9_]*$ "
                    "(uppercase letters, digits, underscores; must start with a letter)."
                ),
                is_error=True,
            )

        from shared import repo_secrets

        value = await repo_secrets.get(
            context.repo_id,
            key,
            organization_id=context.organization_id,
        )

        if value is None:
            output = json.dumps({"key": key, "value": None, "set": False})
        else:
            output = json.dumps({"key": key, "value": value, "set": True})

        return ToolResult(output=output, token_estimate=len(output) // 3)
