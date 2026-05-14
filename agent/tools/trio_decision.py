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


class SubmitClarificationTool(_SinkTool):
    name = "submit_clarification"
    description = (
        "Pause the trio and ask the human a question. Use this ONLY when "
        "you genuinely cannot proceed without an answer. Phrase the question "
        "as a numbered markdown list if you have multiple things to ask. "
        "Do not call this AND submit_backlog in the same run — pick one."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to surface to the human.",
            },
        },
        "required": ["question"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        q = str(arguments.get("question", "")).strip()
        if not q:
            return ToolResult(
                output="submit_clarification: 'question' must be non-empty.",
                is_error=True,
            )
        self._sink.clarification = q
        self._sink.log.append({"tool": "submit_clarification"})
        return ToolResult(
            output="Clarification recorded. The parent will be paused. Stop here.",
            token_estimate=10,
        )


class SubmitCheckpointDecisionTool(_SinkTool):
    name = "submit_checkpoint_decision"
    description = (
        "Commit the architect's checkpoint decision after reviewing the "
        "current state of the integration branch. Use one of:\n"
        "- 'done' — backlog complete, ready to open the integration PR.\n"
        "- 'continue' — keep going; next pending item is fine to dispatch.\n"
        "- 'revise' — backlog needs new items; call run_revision next.\n"
        "- 'blocked' — cannot proceed; supply 'reason'.\n"
        "- 'awaiting_clarification' — need human input; supply 'question'.\n"
        "Call EXACTLY ONCE per checkpoint run."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["done", "continue", "revise", "blocked", "awaiting_clarification"],
            },
            "reason": {
                "type": "string",
                "description": "One sentence explaining the decision.",
            },
            "question": {
                "type": "string",
                "description": "For 'awaiting_clarification' only.",
            },
        },
        "required": ["action"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        action = str(arguments.get("action", "")).strip()
        valid = {"done", "continue", "revise", "blocked", "awaiting_clarification"}
        if action not in valid:
            return ToolResult(
                output=f"submit_checkpoint_decision: 'action' must be one of {sorted(valid)}.",
                is_error=True,
            )
        decision: dict = {"action": action}
        reason = str(arguments.get("reason", "")).strip()
        if reason:
            decision["reason"] = reason
        question = str(arguments.get("question", "")).strip()
        if question:
            decision["question"] = question
        self._sink.checkpoint = decision
        self._sink.log.append({"tool": "submit_checkpoint_decision", "action": action})
        return ToolResult(
            output=f"Checkpoint decision recorded: {action}. Stop here.",
            token_estimate=10,
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


class SubmitTiebreakTool(_SinkTool):
    name = "submit_tiebreak_decision"
    description = (
        "As the architect tiebreaker, commit your decision after reading "
        "the coder↔reviewer transcript. One of:\n"
        "- 'accept' — coder is right; mark this item done.\n"
        "- 'redo' — reviewer is right; supply 'guidance' for the next coder run.\n"
        "- 'revise_backlog' — the item itself is wrong; supply 'new_items'.\n"
        "- 'clarify' — escalate to human; supply 'question'.\n"
        "Call EXACTLY ONCE."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["accept", "redo", "revise_backlog", "clarify"],
            },
            "reason": {"type": "string"},
            "guidance": {
                "type": "string",
                "description": "For 'redo' — specific instructions for the next coder.",
            },
            "new_items": {
                "type": "array",
                "description": "For 'revise_backlog' — replacement work items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["id", "title", "description"],
                },
            },
            "question": {
                "type": "string",
                "description": "For 'clarify' — the question for the human.",
            },
        },
        "required": ["action"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        action = str(arguments.get("action", "")).strip()
        valid = {"accept", "redo", "revise_backlog", "clarify"}
        if action not in valid:
            return ToolResult(
                output=f"submit_tiebreak_decision: 'action' must be one of {sorted(valid)}.",
                is_error=True,
            )
        decision: dict = {"action": action}
        for key in ("reason", "guidance", "question"):
            v = str(arguments.get(key, "")).strip()
            if v:
                decision[key] = v
        new_items = arguments.get("new_items")
        if isinstance(new_items, list) and new_items:
            decision["new_items"] = [
                {
                    "id": str(i.get("id", "")),
                    "title": str(i.get("title", "")),
                    "description": str(i.get("description", "")),
                }
                for i in new_items
                if isinstance(i, dict)
            ]
        self._sink.tiebreak = decision
        self._sink.log.append({"tool": "submit_tiebreak_decision", "action": action})
        return ToolResult(
            output=f"Tiebreak decision recorded: {action}. Stop here.",
            token_estimate=10,
        )


def attach_architect_decision_tools(
    agent: Any, sink: DecisionSink, *, allow_clarification: bool = True
) -> DecisionSink:
    """Register submit_backlog (+ optionally submit_clarification) on the
    given architect agent. Returns the same sink for chaining."""
    agent.tools.register(SubmitBacklogTool(sink))
    if allow_clarification:
        agent.tools.register(SubmitClarificationTool(sink))
    return sink


def attach_checkpoint_decision_tool(agent: Any, sink: DecisionSink) -> DecisionSink:
    agent.tools.register(SubmitCheckpointDecisionTool(sink))
    return sink


def attach_review_verdict_tool(agent: Any, sink: DecisionSink) -> DecisionSink:
    agent.tools.register(SubmitReviewVerdictTool(sink))
    return sink


def attach_tiebreak_tool(agent: Any, sink: DecisionSink) -> DecisionSink:
    agent.tools.register(SubmitTiebreakTool(sink))
    return sink
