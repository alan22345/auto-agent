"""Phase 2 LLM labelling for flows and capabilities (spec §4).

Public entry point is :func:`label_flow_blob`. It composes:

  per-flow labelling   ->  per-capability grouping + labelling
       |                          |
  cache by file_set_hash    cache by flow_membership_hash

The labelled :class:`shared.types.FlowJsonBlob` is returned; the caller
(the recompute endpoint) persists it. The labeller is async and uses
:func:`agent.llm.structured.complete_json` for one-shot JSON output.

Cost discipline:
* Per-flow LLM calls cap source slices at ``MAX_LINES_PER_STEP`` lines.
* Total per-flow prompt tokens are bounded by the slice cap x step count.
* Capability grouping is a single LLM call over all flow summaries.
* Reuses prior labels whose ``file_set_hash`` / ``flow_membership_hash``
  match the input blob -- the cache key contract from Phase 1 §4.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from agent.llm.structured import complete_json
from agent.llm.types import Message

if TYPE_CHECKING:
    from pathlib import Path

    from agent.llm.base import LLMProvider
    from shared.types import Flow, FlowStep, Node

log = structlog.get_logger(__name__)

#: Maximum source-line span included in the LLM prompt for one step.
#: Functions longer than this are truncated head-only — the leading lines
#: tend to carry the signature + docstring + early returns, which is
#: enough signal for naming.
MAX_LINES_PER_STEP = 40


def _load_file_slices(
    workspace_root: Path,
    steps: list[FlowStep],
    nodes_by_id: dict[str, Node],
    *,
    max_lines_per_step: int = MAX_LINES_PER_STEP,
) -> list[dict[str, object]]:
    """Read source slices for each step in *steps* from *workspace_root*.

    Returns a list of records ``{"file", "lines": [start, end], "content"}``,
    one per unique ``(file, line_start, line_end)`` triple. Skips steps
    whose node has no file, no line range, or whose file doesn't exist
    on disk. Line ranges longer than ``max_lines_per_step`` are head-
    truncated.
    """
    seen: set[tuple[str, int, int]] = set()
    out: list[dict[str, object]] = []
    for step in steps:
        node = nodes_by_id.get(step.node_id)
        if node is None:
            continue
        if not node.file or node.line_start is None or node.line_end is None:
            continue
        key = (node.file, node.line_start, node.line_end)
        if key in seen:
            continue
        seen.add(key)

        file_path = workspace_root / node.file
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines = text.splitlines()
        # File lines are 1-indexed; slice is [start-1, end] (end inclusive).
        start_zero = max(0, node.line_start - 1)
        end_zero = min(len(lines), node.line_end)
        clipped_end = min(end_zero, start_zero + max_lines_per_step)
        content_lines = lines[start_zero:clipped_end]
        content = "\n".join(content_lines) + "\n"
        out.append(
            {
                "file": node.file,
                "lines": [node.line_start, node.line_start + len(content_lines) - 1],
                "content": content,
            },
        )
    return out


#: Maximum tokens for a per-flow naming response. The output is tiny
#: (a name + one sentence), so a tight cap prevents the model from
#: padding with reasoning. Mirrors gap_fill.py's choice.
_FLOW_LABEL_MAX_TOKENS = 256

_FLOW_LABEL_SYSTEM = (
    "You name code flows for a developer-facing repo map. Each flow is a "
    "trace from an entry point (HTTP route, queue handler, CLI command, "
    "scheduled job) to a terminal side effect.\n\n"
    "Given the entry point, terminal kind, and source-code slices, "
    "return JSON exactly:\n"
    '{"name": "<<=4 words, Title Case>", '
    '"description": "<<=25 words, one sentence>"}\n\n'
    "The name should be product-language (what the user does), not "
    "function names. Bad: \"login function\". Good: \"Google OAuth Login\"."
)


async def _label_flow(
    provider: LLMProvider,
    flow: Flow,
    slices: list[dict[str, object]],
) -> tuple[str | None, str | None]:
    """Ask the LLM to name *flow* given its source slices.

    Returns ``(name, description)``. Returns ``(None, None)`` if the LLM
    call fails (parse error, empty strings) — caller leaves the flow
    unlabelled rather than fabricating a name.
    """
    payload = {
        "entry_point": flow.entry_point.node_id,
        "entry_kind": flow.entry_point.kind,
        "terminal_kind": flow.terminal_kind,
        "step_labels": [s.node_id for s in flow.steps[:10]],
        "source_slices": slices,
    }
    user_msg = Message(role="user", content=str(payload))

    try:
        response = await complete_json(
            provider,
            messages=[user_msg],
            system=_FLOW_LABEL_SYSTEM,
            max_tokens=_FLOW_LABEL_MAX_TOKENS,
            temperature=0.0,
            retries=2,
        )
    except ValueError as exc:
        log.warning("flow_label.parse_failed", flow_id=flow.id, error=str(exc))
        return (None, None)

    name = response.get("name") or None
    description = response.get("description") or None
    if not name or not description:
        log.warning("flow_label.empty_response", flow_id=flow.id, response=response)
        return (None, None)
    return (name, description)


__all__ = ["MAX_LINES_PER_STEP", "_label_flow", "_load_file_slices"]
