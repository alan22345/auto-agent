"""Builder-side tool to consult the architect mid-build on design questions."""
from __future__ import annotations

from typing import Any, ClassVar

from agent.tools.base import Tool, ToolContext, ToolResult


# Imported lazily so tests can patch the symbol on this module without dragging
# the full architect module into tool-registry import time.
async def architect_consult(*, parent_task_id: int, child_task_id: int, question: str, why: str):
    from agent.lifecycle.trio.architect import consult as _consult
    return await _consult(
        parent_task_id=parent_task_id,
        child_task_id=child_task_id,
        question=question,
        why=why,
    )


class ConsultArchitectTool(Tool):
    name = "consult_architect"
    description = (
        "Ask the architect a clarification question about the design. Use this "
        "when you hit an ambiguity that touches design — file layout, data "
        "model, abstraction choice — not for code-local decisions. The "
        "architect has the full ARCHITECTURE.md and prior context. Returns "
        "the architect's answer, plus a note if ARCHITECTURE.md was updated "
        "as a result (re-read it before continuing)."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The specific question."},
            "why": {"type": "string", "description": "Why you need this — what's blocking you."},
        },
        "required": ["question", "why"],
    }
    is_readonly = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.parent_task_id is None:
            return ToolResult(
                output="consult_architect is only available to trio child tasks.",
                is_error=True,
            )

        payload = await architect_consult(
            parent_task_id=context.parent_task_id,
            child_task_id=context.task_id,
            question=arguments["question"],
            why=arguments["why"],
        )

        # Backward-compat: architect_consult may return a bare answer string or a dict.
        if isinstance(payload, dict):
            answer = payload["answer"]
            updated = payload.get("architecture_md_updated", False)
        else:
            answer = payload
            updated = False

        prefix = "Note: ARCHITECTURE.md was updated; re-read it before continuing.\n\n" if updated else ""
        return ToolResult(output=prefix + answer)
