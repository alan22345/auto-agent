"""Gap-fill tests (ADR-016 Phase 3 §gap_fill).

``gap_fill_site`` is the one-shot LLM call per unresolved dispatch site.
With a mocked provider returning canned JSON we exercise:

* canned edge → returned with ``source_kind="llm"``;
* empty edges list → empty result;
* edges with malformed entries → those entries silently dropped (Pydantic
  validation failure) but the well-formed ones survive;
* the system prompt includes the candidate node ids and the surrounding
  code window.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer.gap_fill import gap_fill_site
from agent.graph_analyzer.types import UnresolvedSite
from agent.llm.types import LLMResponse, Message
from shared.types import Node


def _provider_returning(payload: dict | str) -> MagicMock:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            message=Message(role="assistant", content=body),
            stop_reason="end_turn",
        ),
    )
    return provider


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


@pytest.mark.asyncio
async def test_returns_edge_with_llm_source_kind(tmp_path: Path) -> None:
    provider = _provider_returning(
        {
            "edges": [
                {
                    "target_node_id": "agent/handlers.py::ping_handler",
                    "evidence_line": 42,
                    "evidence_snippet": "handler = HANDLERS[event_type]",
                },
            ],
        },
    )
    edges = await gap_fill_site(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert len(edges) == 1
    e = edges[0]
    assert e.source == "agent/loop.py::dispatch"
    assert e.target == "agent/handlers.py::ping_handler"
    assert e.kind == "calls"
    assert e.source_kind == "llm"
    assert e.evidence.file == "agent/loop.py"
    assert e.evidence.line == 42
    assert e.evidence.snippet == "handler = HANDLERS[event_type]"


@pytest.mark.asyncio
async def test_empty_edges_returns_empty_list(tmp_path: Path) -> None:
    provider = _provider_returning({"edges": []})
    edges = await gap_fill_site(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []


@pytest.mark.asyncio
async def test_malformed_entries_are_skipped(tmp_path: Path) -> None:
    # First entry missing target_node_id, second well-formed.
    provider = _provider_returning(
        {
            "edges": [
                {"evidence_line": 1, "evidence_snippet": "x"},
                {
                    "target_node_id": "agent/handlers.py::ping_handler",
                    "evidence_line": 42,
                    "evidence_snippet": "handler = HANDLERS[event_type]",
                },
            ],
        },
    )
    edges = await gap_fill_site(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert len(edges) == 1
    assert edges[0].target == "agent/handlers.py::ping_handler"


@pytest.mark.asyncio
async def test_provider_returns_garbage_returns_empty(tmp_path: Path) -> None:
    # complete_json raises ValueError after retries on unparseable
    # output. Gap-fill must catch that and return [].
    provider = _provider_returning("not json at all")
    edges = await gap_fill_site(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []


@pytest.mark.asyncio
async def test_system_prompt_includes_candidates_and_surrounding(tmp_path: Path) -> None:
    provider = _provider_returning({"edges": []})
    site = _site()
    await gap_fill_site(
        provider=provider,
        workspace_path=str(tmp_path),
        site=site,
        candidate_nodes=[
            _node("agent/handlers.py::ping_handler"),
            _node("agent/handlers.py::pong_handler"),
        ],
    )
    kwargs = provider.complete.await_args_list[0].kwargs
    system = kwargs["system"]
    assert "agent/handlers.py::ping_handler" in system
    assert "agent/handlers.py::pong_handler" in system
    # Surrounding code surfaces too.
    user_msg = kwargs["messages"][0].content
    assert "HANDLERS[event_type]" in user_msg


@pytest.mark.asyncio
async def test_candidate_pool_capped_at_150(tmp_path: Path) -> None:
    """Cost discipline — too-large candidate lists balloon tokens. The
    prompt must cap at 150 entries even if the caller hands more."""
    provider = _provider_returning({"edges": []})
    nodes = [_node(f"agent/m.py::fn{i}") for i in range(300)]
    await gap_fill_site(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=nodes,
    )
    kwargs = provider.complete.await_args_list[0].kwargs
    system = kwargs["system"]
    # We don't want all 300 ids in the prompt; the first 150 should
    # appear, the 200th should not.
    assert "agent/m.py::fn0" in system
    assert "agent/m.py::fn200" not in system


@pytest.mark.asyncio
async def test_target_outside_candidate_list_is_silently_dropped(tmp_path: Path) -> None:
    """The LLM is told to stay inside the candidate list; if it cites
    something outside it, gap-fill drops that edge before returning.
    Belt-and-braces — citation/target validation in the pipeline catches
    it too, but failing fast here keeps the validator's logs clean."""
    provider = _provider_returning(
        {
            "edges": [
                {
                    "target_node_id": "agent/imaginary.py::nope",
                    "evidence_line": 42,
                    "evidence_snippet": "handler = HANDLERS[event_type]",
                },
            ],
        },
    )
    edges = await gap_fill_site(
        provider=provider,
        workspace_path=str(tmp_path),
        site=_site(),
        candidate_nodes=[_node("agent/handlers.py::ping_handler")],
    )
    assert edges == []
