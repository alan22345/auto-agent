"""ADR-writing tool for the architect agent."""
from __future__ import annotations

import os
import re
from typing import Any, ClassVar

from agent.tools.base import Tool, ToolContext, ToolResult


def _slug(title: str) -> str:
    """Lowercase, alphanumerics + hyphens only, <= 40 chars."""
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:40] or "decision"


def _next_number(decisions_dir: str) -> int:
    """Find the highest existing NNN- prefix and return NNN+1 (skipping 000)."""
    if not os.path.isdir(decisions_dir):
        return 1
    nums = []
    for name in os.listdir(decisions_dir):
        m = re.match(r"^(\d{3})-", name)
        if m and int(m.group(1)) != 0:  # 000 is the template
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def _render(template: str, title: str, context: str, decision: str, consequences: str) -> str:
    """Render the ADR body.

    Tries ``str.format`` against the template first (works when the template
    uses ``{title}``, ``{context}``, ``{decision}``, ``{consequences}``
    placeholders).  Falls back to a canonical ADR structure when the template
    is the project's prose template (which has no format keys).
    """
    try:
        return template.format(
            title=title,
            context=context,
            decision=decision,
            consequences=consequences,
        )
    except (KeyError, ValueError):
        # Real project template uses prose section headers — build directly.
        return (
            f"# {title}\n\n"
            "## Status\n\nAccepted\n\n"
            f"## Context\n\n{context}\n\n"
            f"## Decision\n\n{decision}\n\n"
            f"## Consequences\n\n{consequences}\n"
        )


class RecordDecisionTool(Tool):
    name = "record_decision"
    description = (
        "Record a non-obvious design tradeoff as an ADR in docs/decisions/. "
        "Use this whenever you make a decision the human reviewer would want "
        "to see the rationale for. The ADR is committed alongside any related "
        "ARCHITECTURE.md edits. Returns the path of the new ADR file."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "context": {"type": "string"},
            "decision": {"type": "string"},
            "consequences": {"type": "string"},
        },
        "required": ["title", "context", "decision", "consequences"],
    }
    is_readonly = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        decisions_dir = os.path.join(context.workspace, "docs", "decisions")
        template_path = os.path.join(decisions_dir, "000-template.md")
        if not os.path.isfile(template_path):
            return ToolResult(
                output=(
                    f"ADR template not found at {template_path}. "
                    "Create docs/decisions/000-template.md first."
                ),
                is_error=True,
            )

        with open(template_path) as f:
            template = f.read()

        n = _next_number(decisions_dir)
        slug = _slug(arguments["title"])
        filename = f"{n:03d}-{slug}.md"
        path = os.path.join(decisions_dir, filename)

        body = _render(
            template,
            title=arguments["title"],
            context=arguments["context"],
            decision=arguments["decision"],
            consequences=arguments["consequences"],
        )

        with open(path, "w") as f:
            f.write(body)

        return ToolResult(output=f"Wrote ADR: docs/decisions/{filename}", token_estimate=20)
