"""Bounded agent-loop escape for unresolved dispatch sites (ADR-016 Phase 3 §4).

Triggered by the pipeline when the one-shot :func:`gap_fill_site` call
returns zero validatable edges. Gives the LLM a narrow toolset and
hard bounds so that one site never balloons into an unbounded
investigation:

Tools exposed to the LLM:

* ``read_file(path)`` — read up to 50 KB of workspace-relative file.
* ``grep(pattern, path=None)`` — ripgrep-style search; up to 100
  matches.
* ``return_findings(edges)`` — terminal; emits edges in the same JSON
  schema as :func:`gap_fill_site`.

Hard caps:

* :data:`AGENT_ESCAPE_MAX_ITERATIONS` — 5 model turns max.
* :data:`AGENT_ESCAPE_MAX_SECONDS` — 60 s wall-clock max.
* :data:`AGENT_ESCAPE_MAX_TOTAL_INPUT_TOKENS` — 32 000 cumulative.
* :data:`AGENT_ESCAPE_MAX_TOTAL_OUTPUT_TOKENS` — 4 000 cumulative.

The loop is intentionally *not* a thin shim around :class:`agent.loop.AgentLoop`.
That loop imports the world (session, context manager, cache,
microcompact, …) and the escape's scope is narrow. Reimplementing the
small slice we need keeps the cost profile predictable.

Edges the agent emits are subject to the **same** unconditional
citation/target validation as the one-shot path — see
``agent/graph_analyzer/validator.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from agent.graph_analyzer.gap_fill import _coerce_payload_to_edges
from agent.llm.types import Message, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from agent.graph_analyzer.types import UnresolvedSite
    from agent.llm.base import LLMProvider
    from shared.types import Edge, Node

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Hard caps — locked by the Phase 3 brief; changing any of these is a
# conscious decision flagged by ``test_module_constants_match_phase3_brief``.
# ---------------------------------------------------------------------------

AGENT_ESCAPE_MAX_ITERATIONS = 5
AGENT_ESCAPE_MAX_SECONDS = 60
AGENT_ESCAPE_MAX_TOTAL_INPUT_TOKENS = 32_000
AGENT_ESCAPE_MAX_TOTAL_OUTPUT_TOKENS = 4_000

# Per-tool bounds (also brief-mandated).
_READ_FILE_MAX_BYTES = 50 * 1024
_GREP_MAX_MATCHES = 100

# Output-token budget for a single model call inside the loop. The
# *cumulative* total is bounded above; this caps a single response.
_PER_CALL_MAX_OUTPUT_TOKENS = 1024


# ---------------------------------------------------------------------------
# Time indirection — tests monkey-patch this rather than ``time.monotonic``
# to avoid touching global state.
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# Tool definitions sent to the model. Schemas are tiny on purpose —
# anything not pinned here is illegal input.
# ---------------------------------------------------------------------------


def _tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="read_file",
            description=(
                "Read a UTF-8 text file from the workspace. Path is "
                f"workspace-relative. Output capped at {_READ_FILE_MAX_BYTES} bytes "
                "with a truncation marker if the file is larger."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path."},
                },
                "required": ["path"],
            },
        ),
        ToolDefinition(
            name="grep",
            description=(
                "Search files for a regex pattern. Returns up to "
                f"{_GREP_MAX_MATCHES} matches as {{file, line, snippet}} objects. "
                "Path is optional; defaults to the whole workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        ),
        ToolDefinition(
            name="return_findings",
            description=(
                "Terminal tool. Emit zero or more edges and stop. Same JSON "
                "schema as one-shot gap-fill: "
                '{"edges": [{"target_node_id": ..., "evidence_line": ..., '
                '"evidence_snippet": ...}, ...]}.'
            ),
            parameters={
                "type": "object",
                "properties": {
                    "edges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target_node_id": {"type": "string"},
                                "evidence_line": {"type": "integer"},
                                "evidence_snippet": {"type": "string"},
                            },
                            "required": [
                                "target_node_id",
                                "evidence_line",
                                "evidence_snippet",
                            ],
                        },
                    },
                },
                "required": ["edges"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers — workspace-pinned. They never escape the workspace and
# never write.
# ---------------------------------------------------------------------------


def _safe_join(workspace_path: str, rel_path: str) -> str | None:
    """Join + canonicalise; return None if the result would escape the
    workspace. Mirrors the policy in ``agent/tools/base.py::ToolContext.resolve``
    but is intentionally stand-alone — the escape's tools are scoped
    narrowly enough that we don't want to bring the full ToolContext
    machinery in here."""
    ws_real = os.path.realpath(workspace_path)
    candidate = os.path.realpath(os.path.join(workspace_path, rel_path))
    if candidate == ws_real or candidate.startswith(ws_real + os.sep):
        return candidate
    return None


def _tool_read_file(workspace_path: str, args: dict) -> str:
    path = args.get("path")
    if not isinstance(path, str):
        return "error: read_file requires a string 'path' argument."
    resolved = _safe_join(workspace_path, path)
    if resolved is None or not os.path.isfile(resolved):
        return f"error: file not found: {path}"
    try:
        with open(resolved, "rb") as fh:
            raw = fh.read(_READ_FILE_MAX_BYTES + 1)
    except OSError as e:
        return f"error: failed to read {path}: {e}"
    if len(raw) > _READ_FILE_MAX_BYTES:
        body = raw[:_READ_FILE_MAX_BYTES].decode("utf-8", errors="replace")
        return body + f"\n\n[...truncated at {_READ_FILE_MAX_BYTES} bytes]"
    return raw.decode("utf-8", errors="replace")


def _tool_grep(workspace_path: str, args: dict) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return "error: grep requires a non-empty 'pattern' argument."
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"error: invalid regex: {e}"

    rel_path = args.get("path")
    if isinstance(rel_path, str) and rel_path:
        root = _safe_join(workspace_path, rel_path)
        if root is None:
            return f"error: path escapes workspace: {rel_path}"
    else:
        root = os.path.realpath(workspace_path)

    matches: list[dict[str, Any]] = []
    if os.path.isfile(root):
        files_to_scan: list[str] = [root]
    else:
        files_to_scan = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip common heavy dirs for performance.
            dirnames[:] = [
                d
                for d in dirnames
                if d not in {".git", "node_modules", ".venv", "__pycache__"}
            ]
            for fn in filenames:
                files_to_scan.append(os.path.join(dirpath, fn))
    for fp in files_to_scan:
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if regex.search(line):
                        rel = os.path.relpath(fp, workspace_path)
                        matches.append(
                            {
                                "file": rel.replace(os.sep, "/"),
                                "line": lineno,
                                "snippet": line.rstrip("\n"),
                            },
                        )
                        if len(matches) >= _GREP_MAX_MATCHES:
                            break
        except OSError:
            continue
        if len(matches) >= _GREP_MAX_MATCHES:
            break
    return json.dumps({"matches": matches})


def _dispatch_tool(tool: ToolCall, workspace_path: str) -> str:
    """Run one tool call and return the textual result. Unknown tools
    surface a structured error string so the LLM can recover."""
    if tool.name == "read_file":
        return _tool_read_file(workspace_path, tool.arguments or {})
    if tool.name == "grep":
        return _tool_grep(workspace_path, tool.arguments or {})
    if tool.name == "return_findings":
        # Should be handled by the main loop, not here.
        return "error: return_findings is terminal — the loop should handle it."
    return f"error: unknown tool: {tool.name}"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def _build_system_prompt(
    site: UnresolvedSite,
    candidate_ids: list[str],
) -> str:
    bulleted = "\n".join(f"- {nid}" for nid in candidate_ids)
    return (
        "You are resolving a single dynamic-dispatch call site in a "
        "code graph after the one-shot pass could not.\n"
        "\n"
        "You have three tools:\n"
        " - read_file(path): read a workspace-relative file (capped at 50KB).\n"
        " - grep(pattern, path=None): regex search, up to 100 matches.\n"
        " - return_findings(edges): terminal. Emit edges and stop.\n"
        "\n"
        "Use the tools sparingly. If you cannot prove a target with a "
        "real citation (file/line/snippet that exists in the workspace), "
        "call return_findings with an empty edges array.\n"
        "\n"
        "Each edge you return must:\n"
        "1. Have target_node_id in the candidate list.\n"
        "2. Cite the line where the call happens.\n"
        "3. Include a snippet that is a substring of the file at that line.\n"
        "\n"
        f"Dispatch pattern hint: {site.pattern_hint}\n"
        "\n"
        "Candidate target nodes:\n"
        f"{bulleted}\n"
    )


def _build_initial_user_message(site: UnresolvedSite) -> Message:
    return Message(
        role="user",
        content=(
            f"File: {site.file}\n"
            f"Containing function id: {site.containing_node_id}\n"
            f"Dispatch site (line {site.line}): {site.snippet}\n"
            "\n"
            "Surrounding source:\n"
            "```\n"
            f"{site.surrounding_code}\n"
            "```\n"
        ),
    )


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


@dataclass
class _Budget:
    """Mutable budget tracker shared across the loop's iterations."""

    started_at: float
    input_tokens: int = 0
    output_tokens: int = 0
    iterations: int = 0
    bounds_reasons: list[str] = field(default_factory=list)

    def exceeded(self) -> str | None:
        if self.iterations >= AGENT_ESCAPE_MAX_ITERATIONS:
            return "max_iterations"
        elapsed = _now() - self.started_at
        if elapsed >= AGENT_ESCAPE_MAX_SECONDS:
            return "max_wall_clock"
        if self.input_tokens >= AGENT_ESCAPE_MAX_TOTAL_INPUT_TOKENS:
            return "max_input_tokens"
        if self.output_tokens >= AGENT_ESCAPE_MAX_TOTAL_OUTPUT_TOKENS:
            return "max_output_tokens"
        return None


async def agent_escape(
    provider: LLMProvider,
    workspace_path: str,
    site: UnresolvedSite,
    candidate_nodes: list[Node],
) -> list[Edge]:
    """Run the bounded mini-loop for one site.

    Returns the list of LLM-emitted edges (each tagged
    ``source_kind="llm"``). The caller is responsible for the
    unconditional citation/target validation — this function applies
    only the same "target must be in the candidate pool" soft filter
    as :func:`gap_fill_site` so the schemas line up.
    """
    # Cap candidate pool — same as gap-fill.
    from agent.graph_analyzer.gap_fill import _CANDIDATE_CAP

    bounded = candidate_nodes[:_CANDIDATE_CAP]
    candidate_ids = [n.id for n in bounded]
    id_set = set(candidate_ids)

    system = _build_system_prompt(site, candidate_ids)
    messages: list[Message] = [_build_initial_user_message(site)]
    tools = _tool_definitions()

    budget = _Budget(started_at=_now())
    findings: list[Edge] = []

    while True:
        reason = budget.exceeded()
        if reason is not None:
            log.info(
                "graph_agent_escape_bound_hit",
                site=site.containing_node_id,
                reason=reason,
                iterations=budget.iterations,
            )
            break

        try:
            response = await provider.complete(
                messages=messages,
                tools=tools,
                system=system,
                max_tokens=_PER_CALL_MAX_OUTPUT_TOKENS,
                temperature=0.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "graph_agent_escape_provider_error",
                site=site.containing_node_id,
                error=str(e),
                error_type=e.__class__.__name__,
            )
            break

        budget.iterations += 1
        budget.input_tokens += response.usage.input_tokens
        budget.output_tokens += response.usage.output_tokens

        # Append assistant turn to the running message history.
        messages.append(response.message)

        tool_calls = response.message.tool_calls or []
        if not tool_calls:
            # Model ended its turn without calling return_findings —
            # nothing to add.
            log.info(
                "graph_agent_escape_no_tool_call",
                site=site.containing_node_id,
                stop_reason=response.stop_reason,
            )
            break

        # Handle each tool call. The first ``return_findings`` is
        # terminal; later tool calls in the same turn are ignored.
        terminated = False
        for tc in tool_calls:
            if tc.name == "return_findings":
                payload = tc.arguments if isinstance(tc.arguments, dict) else {}
                findings = _coerce_payload_to_edges(payload, site, id_set)
                terminated = True
                break
            tool_result = _dispatch_tool(tc, workspace_path)
            messages.append(
                Message(
                    role="tool",
                    content=tool_result,
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                ),
            )
        if terminated:
            break

    return findings


__all__ = [
    "AGENT_ESCAPE_MAX_ITERATIONS",
    "AGENT_ESCAPE_MAX_SECONDS",
    "AGENT_ESCAPE_MAX_TOTAL_INPUT_TOKENS",
    "AGENT_ESCAPE_MAX_TOTAL_OUTPUT_TOKENS",
    "agent_escape",
]
