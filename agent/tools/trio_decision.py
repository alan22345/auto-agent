"""Decision-submission tools for the trio architect + reviewer agents.

Replaces "agent writes a JSON block at the end of its reasoning, we
regex it out" with "agent calls a tool to commit its decision". JSON
extraction at the end of a long reasoning response is fragile — the
model often skips the envelope after spending its turn on the
analysis. Tool calls are structured by construction.

Each tool writes its arguments into a caller-supplied ``DecisionSink``
dataclass. The caller (architect / dispatcher) reads the sink after
``agent.run()`` returns. The tools themselves are stateless beyond
the sink reference; one fresh sink per agent invocation.

A single ``DecisionSink`` carries every shape we need — backlog,
clarification, checkpoint, reviewer verdict, tiebreak — because in
any given agent run at most one is set. The shapes don't conflict
and a flat dataclass is easier to reason about than five sibling
classes. Unused fields stay ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import structlog

from agent.tools.base import Tool, ToolContext, ToolResult

log = structlog.get_logger()


@dataclass
class DecisionSink:
    """Shared scratchpad for all submit_X tools in one agent run.

    Only one of the fields below should be populated in any given run;
    the caller picks the relevant one based on which path the agent
    was supposed to take. Multiple submissions overwrite (last call
    wins); the tool's return message tells the agent to call only
    once.
    """

    backlog: list[dict] | None = None
    clarification: str | None = None
    checkpoint: dict | None = None
    review_verdict: dict | None = None
    tiebreak: dict | None = None
    # The submission history — useful for debugging double-submits.
    log: list[dict] = field(default_factory=list)


class _SinkTool(Tool):
    """Shared base for tools that just write to a DecisionSink."""

    name: ClassVar[str] = ""
    is_readonly: ClassVar[bool] = False

    def __init__(self, sink: DecisionSink):
        self._sink = sink

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        raise NotImplementedError


class SubmitBacklogTool(_SinkTool):
    name = "submit_backlog"
    description = (
        "Commit the architect's backlog for this trio cycle. Call this "
        "EXACTLY ONCE when the architecture pass is complete and you "
        "have a concrete list of work items the builders should pick up. "
        "If you need to ask the human a question before you can write a "
        "backlog, call submit_clarification instead."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": (
                    "Ordered list of work items. Each item is one focused "
                    "change a builder subagent will pick up. Builders run "
                    "in the parent's slot — no separate PRs per item."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Short stable id, e.g. 'T1'.",
                        },
                        "title": {"type": "string"},
                        "description": {
                            "type": "string",
                            "description": (
                                "Full instructions for the builder. "
                                "Include any context they need — they "
                                "do not see your reasoning."
                            ),
                        },
                    },
                    "required": ["id", "title", "description"],
                },
            },
        },
        "required": ["items"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        items = arguments.get("items") or []
        if not isinstance(items, list) or not items:
            return ToolResult(
                output="submit_backlog: 'items' must be a non-empty list.",
                is_error=True,
            )
        normalised: list[dict] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            normalised.append(
                {
                    "id": str(raw.get("id", "")) or f"T{len(normalised) + 1}",
                    "title": str(raw.get("title", "")),
                    "description": str(raw.get("description", "")),
                    "status": "pending",
                }
            )
        if not normalised:
            return ToolResult(
                output="submit_backlog: every item must be an object with id/title/description.",
                is_error=True,
            )
        self._sink.backlog = normalised
        self._sink.log.append({"tool": "submit_backlog", "count": len(normalised)})
        return ToolResult(
            output=f"Backlog of {len(normalised)} items recorded. Stop here.",
            token_estimate=15,
        )


class SubmitReviewVerdictTool(_SinkTool):
    name = "submit_review_verdict"
    description = (
        "Commit the reviewer's verdict on the builder's diff. Call EXACTLY "
        "ONCE at the end of your review. ok=true means the change satisfies "
        "the work item; ok=false means the builder should iterate. Feedback "
        "must be specific and actionable — point at lines, name behaviours, "
        "say what's missing or wrong."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "feedback": {
                "type": "string",
                "description": (
                    "Specific, actionable feedback. Empty for ok=true is fine."
                ),
            },
        },
        "required": ["ok"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        ok = bool(arguments.get("ok"))
        feedback = str(arguments.get("feedback", "")).strip()
        self._sink.review_verdict = {"ok": ok, "feedback": feedback}
        self._sink.log.append({"tool": "submit_review_verdict", "ok": ok})
        return ToolResult(
            output=f"Verdict recorded: ok={ok}. Stop here.",
            token_estimate=10,
        )


def attach_architect_decision_tools(
    agent: Any, sink: DecisionSink
) -> DecisionSink:
    """Register submit_backlog on the given architect agent. Returns the
    same sink for chaining."""
    agent.tools.register(SubmitBacklogTool(sink))
    return sink
