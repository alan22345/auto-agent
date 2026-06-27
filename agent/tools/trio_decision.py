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
