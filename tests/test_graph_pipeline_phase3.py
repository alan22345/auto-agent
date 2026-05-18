"""Pipeline integration tests for ADR-016 Phase 3.

Phase 2 tests in ``test_graph_pipeline.py`` exercise the AST-only path.
These tests pin the new behaviour:

* ``run_pipeline(..., provider=None)`` keeps the Phase 2 behaviour
  bit-for-bit — no LLM call, only AST edges.
* When a ``provider`` is supplied, unresolved sites are funnelled
  through gap-fill (then agent-escape on fallback) and the surviving
  validated edges land in the blob with ``source_kind="llm"``.
* Edges that fail citation or target validation are dropped before they
  reach the blob.
* Escape triggers when one-shot returns zero edges.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from agent.llm.types import LLMResponse, Message, TokenUsage, ToolCall

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_python"


def _setup_workspace(tmp_path: Path) -> str:
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


def _provider_returning_one_shot(payload: dict) -> MagicMock:
    """Provider that always returns the same JSON one-shot reply."""
    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            message=Message(role="assistant", content=json.dumps(payload)),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        ),
    )
    return provider


def _provider_routes_by_tools(
    *,
    gap_fill_payload: dict,
    escape_payload: dict,
) -> MagicMock:
    """Routes provider.complete responses based on whether the call
    supplies tools (escape) or not (gap-fill). This way one mock works
    no matter how many sites the pipeline finds.

    The gap-fill case returns the supplied ``gap_fill_payload`` as JSON.
    The escape case returns a single ``return_findings`` tool call with
    ``escape_payload`` as the arguments dict.
    """
    provider = MagicMock()

    async def respond(*, messages, system, max_tokens, temperature, tools=None):
        if tools:
            return LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="t1",
                            name="return_findings",
                            arguments=escape_payload,
                        ),
                    ],
                ),
                stop_reason="tool_use",
                usage=TokenUsage(input_tokens=10, output_tokens=10),
            )
        return LLMResponse(
            message=Message(
                role="assistant",
                content=json.dumps(gap_fill_payload),
            ),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )

    provider.complete = AsyncMock(side_effect=respond)
    return provider


@pytest.mark.asyncio
async def test_no_provider_skips_gap_fill(tmp_path: Path) -> None:
    """When ``provider=None`` the pipeline behaves exactly as Phase 2 —
    only AST edges, no LLM edges."""
    ws = _setup_workspace(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=None)
    assert all(e.source_kind == "ast" for e in blob.edges)


@pytest.mark.asyncio
async def test_gap_fill_emits_llm_edge_for_registry_site(tmp_path: Path) -> None:
    """The fixture's ``agent_area/registry.py`` has the dispatch site
    ``HANDLERS[name](payload)``. With a mocked provider that cites the
    snippet at the correct line, the blob carries a validated LLM edge."""
    ws = _setup_workspace(tmp_path)
    # The site lives on line 35 of registry.py (the ``return HANDLERS[name](payload)`` line).
    provider = _provider_returning_one_shot(
        {
            "edges": [
                {
                    "target_node_id": "agent_area/registry.py::ping_handler",
                    "evidence_line": 35,
                    "evidence_snippet": "return HANDLERS[name](payload)",
                },
            ],
        },
    )
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    llm_edges = [e for e in blob.edges if e.source_kind == "llm"]
    assert any(
        e.target == "agent_area/registry.py::ping_handler"
        and e.source == "agent_area/registry.py::dispatch"
        and e.evidence.file == "agent_area/registry.py"
        and e.evidence.line == 35
        for e in llm_edges
    )


@pytest.mark.asyncio
async def test_failed_citation_drops_edge(tmp_path: Path) -> None:
    """LLM cites a snippet that does not exist on the cited line (or
    within ±2). Edge must not survive."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot(
        {
            "edges": [
                {
                    "target_node_id": "agent_area/registry.py::ping_handler",
                    "evidence_line": 30,
                    "evidence_snippet": "this snippet is not in the file",
                },
            ],
        },
    )
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    # No LLM edge survives.
    llm_edges = [e for e in blob.edges if e.source_kind == "llm"]
    assert llm_edges == []


@pytest.mark.asyncio
async def test_failed_target_drops_edge(tmp_path: Path) -> None:
    """LLM cites a target id that is not in the graph. The gap-fill
    soft filter would drop it inside the gap_fill module — but the
    unconditional ``validate_target`` is the *load-bearing* check; we
    pin it here by going through the pipeline with a target the gap-
    fill soft filter accidentally let through (we inject via the
    escape path with a candidate-list-accepted-but-graph-absent id).

    Since both filters agree, the easier-to-construct case is: the LLM
    cites a target outside the candidate pool. This validates that the
    final blob has no LLM edge."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot(
        {
            "edges": [
                {
                    "target_node_id": "agent_area/registry.py::nonexistent_target",
                    "evidence_line": 35,
                    "evidence_snippet": "return HANDLERS[name](payload)",
                },
            ],
        },
    )
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    assert [e for e in blob.edges if e.source_kind == "llm"] == []


@pytest.mark.asyncio
async def test_escape_triggers_when_gap_fill_empty(tmp_path: Path) -> None:
    """One-shot returns empty edges → escape is invoked → escape
    returns a valid edge → blob carries it."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_routes_by_tools(
        gap_fill_payload={"edges": []},
        escape_payload={
            "edges": [
                {
                    "target_node_id": "agent_area/registry.py::ping_handler",
                    "evidence_line": 35,
                    "evidence_snippet": "return HANDLERS[name](payload)",
                },
            ],
        },
    )
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    llm_edges = [e for e in blob.edges if e.source_kind == "llm"]
    assert any(e.target == "agent_area/registry.py::ping_handler" for e in llm_edges), llm_edges
    # gap_fill ran per site (≥1), escape ran for each empty gap-fill (≥1).
    assert provider.complete.await_count >= 2


@pytest.mark.asyncio
async def test_ast_edges_preserved_alongside_llm_edges(tmp_path: Path) -> None:
    """The gap-fill stage adds to, does not replace, the AST edge set."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot({"edges": []})  # no LLM edges
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    ast_edges = [e for e in blob.edges if e.source_kind == "ast"]
    # Dog inherits Animal — AST edge must still be there.
    assert any(
        e.kind == "inherits" and e.target == "module:agent_area.base.Animal" for e in ast_edges
    )


@pytest.mark.asyncio
async def test_area_status_counts_unresolved_sites(tmp_path: Path) -> None:
    """``AreaStatus.unresolved_dynamic_sites`` is the count of detected
    sites — independent of whether gap-fill produced edges for them."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot({"edges": []})  # gap-fill no-op
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    statuses = {a.name: a for a in blob.areas}
    # registry.py's HANDLERS[name](payload) is at minimum one site.
    assert statuses["agent_area"].unresolved_dynamic_sites >= 1
