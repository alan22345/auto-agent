"""Agent-escape tests (ADR-016 Phase 3 §agent_escape).

Bounded mini-loop with three tools (``read_file`` / ``grep`` /
``return_findings``) and three hard caps (max iterations, max
wall-clock, max total tokens).

Every test mocks the provider — no real LLM calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer import agent_escape as agent_escape_mod
from agent.graph_analyzer.agent_escape import (
    AGENT_ESCAPE_MAX_ITERATIONS,
    AGENT_ESCAPE_MAX_SECONDS,
    AGENT_ESCAPE_MAX_TOTAL_INPUT_TOKENS,
    AGENT_ESCAPE_MAX_TOTAL_OUTPUT_TOKENS,
    agent_escape,
)
from agent.graph_analyzer.types import UnresolvedSite
from agent.llm.types import LLMResponse, Message, TokenUsage, ToolCall
from shared.types import Node

if TYPE_CHECKING:
    from pathlib import Path

# ----------------------------------------------------------------------
# Test helpers
# ----------------------------------------------------------------------


def _site(**overrides) -> UnresolvedSite:
    base = dict(
        file="agent/loop.py",
        line=42,
        snippet="handler = HANDLERS[event_type]",
        containing_node_id="agent/loop.py::dispatch",
        surrounding_code="def dispatch(event_type, payload):\n    handler = HANDLERS[event_type]\n",
        pattern_hint="registry",
    )
    base.update(overrides)
    return UnresolvedSite(**base)


def _node(node_id: str, *, area: str = "agent") -> Node:
    return Node(id=node_id, kind="function", label=node_id.rsplit(":", 1)[-1], area=area)


def _assistant_with_tool(
    name: str,
    arguments: dict,
    *,
    call_id: str = "call_1",
    input_tokens: int = 10,
    output_tokens: int = 10,
) -> LLMResponse:
    return LLMResponse(
        message=Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        ),
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _assistant_end_turn(text: str = "", **kwargs) -> LLMResponse:
    return LLMResponse(
        message=Message(role="assistant", content=text),
        stop_reason="end_turn",
        usage=TokenUsage(
            input_tokens=kwargs.get("input_tokens", 5),
            output_tokens=kwargs.get("output_tokens", 5),
        ),
    )


def _sequenced_provider(*responses: LLMResponse) -> MagicMock:
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=list(responses))
    return provider


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_valid_edge_via_return_findings(tmp_path: Path) -> None:
    """LLM calls ``return_findings`` directly on iteration 1 — edge
    flows back to the caller."""
    provider = _sequenced_provider(
        _assistant_with_tool(
            "return_findings",
            {
                "edges": [
                    {
                        "target_node_id": "agent/handlers.py::ping_handler",
                        "evidence_line": 42,
                        "evidence_snippet": "handler = HANDLERS[event_type]",
                    },
                ],
            },
        ),
    )
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert len(edges) == 1
    assert edges[0].source == "agent/loop.py::dispatch"
    assert edges[0].target == "agent/handlers.py::ping_handler"
    assert edges[0].source_kind == "llm"


@pytest.mark.asyncio
async def test_read_file_tool_then_return_findings(tmp_path: Path) -> None:
    """LLM reads a file, then returns findings using info it gathered."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "handlers.py").write_text(
        "def ping_handler(payload):\n    return payload\n",
    )

    provider = _sequenced_provider(
        _assistant_with_tool("read_file", {"path": "agent/handlers.py"}, call_id="c1"),
        _assistant_with_tool(
            "return_findings",
            {
                "edges": [
                    {
                        "target_node_id": "agent/handlers.py::ping_handler",
                        "evidence_line": 42,
                        "evidence_snippet": "handler = HANDLERS[event_type]",
                    },
                ],
            },
            call_id="c2",
        ),
    )
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert len(edges) == 1
    # First call has no tool_result; second call's user message contains
    # the read_file output.
    second_user_msgs = provider.complete.await_args_list[1].kwargs["messages"]
    assert any("ping_handler" in (m.content or "") for m in second_user_msgs)


@pytest.mark.asyncio
async def test_grep_tool(tmp_path: Path) -> None:
    """Smoke test for the grep tool."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "handlers.py").write_text(
        "def ping_handler(payload):\n    return payload\n",
    )
    provider = _sequenced_provider(
        _assistant_with_tool("grep", {"pattern": "ping_handler"}, call_id="c1"),
        _assistant_with_tool(
            "return_findings",
            {
                "edges": [
                    {
                        "target_node_id": "agent/handlers.py::ping_handler",
                        "evidence_line": 42,
                        "evidence_snippet": "handler = HANDLERS[event_type]",
                    },
                ],
            },
            call_id="c2",
        ),
    )
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert len(edges) == 1


@pytest.mark.asyncio
async def test_terminates_on_max_iterations(tmp_path: Path) -> None:
    """Provider always asks for more grep — loop must bail at max
    iterations and return whatever edges it has (none, here)."""
    looping_response = _assistant_with_tool("grep", {"pattern": "x"}, call_id="loop")
    # Hand back enough copies that we can sample many iterations.
    provider = MagicMock()
    provider.complete = AsyncMock(return_value=looping_response)
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []
    # Bound holds: at most AGENT_ESCAPE_MAX_ITERATIONS calls.
    assert provider.complete.await_count <= AGENT_ESCAPE_MAX_ITERATIONS


@pytest.mark.asyncio
async def test_terminates_on_wall_clock(monkeypatch, tmp_path: Path) -> None:
    """A slow provider — patch ``_now`` to make wall-clock budget trip
    on iteration 2's pre-call budget check.

    Time samples consumed (in order):
      1. ``_Budget.started_at`` — t=0.
      2. ``budget.exceeded()`` before iter 1's complete() — t=0 (ok).
      3. ``budget.exceeded()`` before iter 2's complete() — t=61 (trips).
    """
    times = iter([0.0, 0.0, AGENT_ESCAPE_MAX_SECONDS + 1.0])
    monkeypatch.setattr(agent_escape_mod, "_now", lambda: next(times))

    provider = _sequenced_provider(
        _assistant_with_tool("grep", {"pattern": "x"}, call_id="c1"),
        _assistant_with_tool("grep", {"pattern": "y"}, call_id="c2"),
    )
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []
    # Loop bailed before the second iteration's complete() call.
    assert provider.complete.await_count == 1


@pytest.mark.asyncio
async def test_terminates_on_token_budget(tmp_path: Path) -> None:
    """First call already consumes the entire input-token budget; the
    second iteration's complete() must not happen."""
    burner = _assistant_with_tool(
        "grep",
        {"pattern": "x"},
        call_id="c1",
        input_tokens=AGENT_ESCAPE_MAX_TOTAL_INPUT_TOKENS + 100,
        output_tokens=10,
    )
    next_one = _assistant_with_tool(
        "grep",
        {"pattern": "y"},
        call_id="c2",
        input_tokens=10,
        output_tokens=10,
    )
    provider = _sequenced_provider(burner, next_one)
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []
    assert provider.complete.await_count == 1


@pytest.mark.asyncio
async def test_terminates_on_output_token_budget(tmp_path: Path) -> None:
    """Same as input-token budget but on the output side."""
    burner = _assistant_with_tool(
        "grep",
        {"pattern": "x"},
        call_id="c1",
        input_tokens=10,
        output_tokens=AGENT_ESCAPE_MAX_TOTAL_OUTPUT_TOKENS + 100,
    )
    next_one = _assistant_with_tool(
        "grep",
        {"pattern": "y"},
        call_id="c2",
        input_tokens=10,
        output_tokens=10,
    )
    provider = _sequenced_provider(burner, next_one)
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []
    assert provider.complete.await_count == 1


@pytest.mark.asyncio
async def test_end_turn_without_findings_returns_empty(tmp_path: Path) -> None:
    """If the LLM ends its turn without calling ``return_findings``,
    the escape produces zero edges."""
    provider = _sequenced_provider(_assistant_end_turn("I have nothing"))
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_and_loop_continues(tmp_path: Path) -> None:
    """LLM calls a tool that doesn't exist — handler returns an error
    string, loop carries on, and eventually it terminates normally."""
    provider = _sequenced_provider(
        _assistant_with_tool("nonsense", {}, call_id="c1"),
        _assistant_with_tool(
            "return_findings",
            {"edges": []},
            call_id="c2",
        ),
    )
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []
    # Both calls happened.
    assert provider.complete.await_count == 2


@pytest.mark.asyncio
async def test_read_file_size_bound(tmp_path: Path) -> None:
    """``read_file`` truncates output past the configured bound. We
    assert via the tool's reply text — the assistant message after the
    read should contain a truncation marker for a large file."""
    big = "x" * (60 * 1024)  # 60KB, above the 50KB bound
    (tmp_path / "big.txt").write_text(big)
    provider = _sequenced_provider(
        _assistant_with_tool("read_file", {"path": "big.txt"}, call_id="c1"),
        _assistant_with_tool("return_findings", {"edges": []}, call_id="c2"),
    )
    await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    # Find the user message after iteration 1 that carries the tool
    # result and check truncation marker.
    second_msgs = provider.complete.await_args_list[1].kwargs["messages"]
    tool_results = [m for m in second_msgs if m.role == "tool"]
    assert tool_results
    assert any("truncated" in (m.content or "").lower() for m in tool_results)


def test_module_constants_match_phase3_brief() -> None:
    """The brief is explicit: 5 iters / 60s / 32k input / 4k output.
    Locking these so a future tweak forces a conscious change."""
    assert AGENT_ESCAPE_MAX_ITERATIONS == 5
    assert AGENT_ESCAPE_MAX_SECONDS == 60
    assert AGENT_ESCAPE_MAX_TOTAL_INPUT_TOKENS == 32_000
    assert AGENT_ESCAPE_MAX_TOTAL_OUTPUT_TOKENS == 4_000


@pytest.mark.asyncio
async def test_target_outside_candidates_dropped(tmp_path: Path) -> None:
    """Same soft filter as gap_fill_site — LLM cites a node not in the
    candidate list, escape drops it."""
    provider = _sequenced_provider(
        _assistant_with_tool(
            "return_findings",
            {
                "edges": [
                    {
                        "target_node_id": "agent/imaginary.py::nope",
                        "evidence_line": 42,
                        "evidence_snippet": "handler = HANDLERS[event_type]",
                    },
                ],
            },
            call_id="c1",
        ),
    )
    edges = await agent_escape(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []
