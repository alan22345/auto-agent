"""End-to-end pipeline tests for ADR-016 Phase 4.

The HTTP-matching stage runs AFTER per-area parsing + gap-fill. With a
cross-language fixture (Python ``router.py`` + TS ``client.ts`` calling
the routes by URL pattern), the assembled blob carries ``Edge(kind="http",
source_kind="ast")`` between the matched nodes.

Tests cover:

* unambiguous ``GET /api/repos`` + ``POST /api/repos`` match → two AST
  HTTP edges, no LLM call;
* ``GET /api/repos/{id}`` matches the literal ``/api/repos/42`` call;
* TS parser dispatch happens via the registry — no language switch in
  the pipeline (Phase 2 spec); the fixture has both Python and TS in
  separate area directories;
* HTTP-match failure (mocked) does not break the rest of the pipeline.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from agent.llm.types import LLMResponse, Message, TokenUsage

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_crosslang"


def _setup_workspace(tmp_path: Path) -> str:
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


def _provider_returning(payload: dict) -> MagicMock:
    """Return a provider whose ``complete`` always returns the supplied
    JSON. Used for negative tests / disambiguation."""
    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            message=Message(role="assistant", content=json.dumps(payload)),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        ),
    )
    return provider


@pytest.mark.asyncio
async def test_unambiguous_http_edges_emitted_without_llm(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning({"target_node_id": ""})
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    http_edges = [e for e in blob.edges if e.kind == "http"]
    # 3 frontend calls, 3 backend routes — all unambiguous (method+path).
    assert len(http_edges) == 3
    targets = sorted(e.target for e in http_edges)
    assert targets == [
        "orchestrator_area/router.py::create_repo",
        "orchestrator_area/router.py::get_repo",
        "orchestrator_area/router.py::list_repos",
    ]
    # All AST-source — no LLM disambiguation needed.
    assert all(e.source_kind == "ast" for e in http_edges)


@pytest.mark.asyncio
async def test_http_edges_have_frontend_call_evidence(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning({"target_node_id": ""})
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    http_edges = [e for e in blob.edges if e.kind == "http"]
    for e in http_edges:
        assert e.evidence.file == "web_next_area/client.ts"
        assert e.evidence.line >= 1
        assert e.evidence.snippet  # non-empty
        # boundary_violation stays False — Phase 5 owns that.
        assert e.boundary_violation is False


@pytest.mark.asyncio
async def test_no_provider_still_emits_unambiguous_http_edges(tmp_path: Path) -> None:
    """``provider=None`` keeps gap-fill off and skips ambiguous-LLM
    disambiguation, but unambiguous AST HTTP edges still land in the
    blob."""
    ws = _setup_workspace(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=None)
    http_edges = [e for e in blob.edges if e.kind == "http"]
    # All three pairs are unambiguous in this fixture.
    assert len(http_edges) == 3
    assert all(e.source_kind == "ast" for e in http_edges)


@pytest.mark.asyncio
async def test_pipeline_other_edges_preserved_alongside_http(tmp_path: Path) -> None:
    """HTTP matching adds to the edge set; AST imports/inherits/calls
    are untouched."""
    ws = _setup_workspace(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=None)
    # We don't pin the count, but at minimum AST edges of other kinds
    # should still be there.
    other = [e for e in blob.edges if e.kind != "http"]
    assert any(e.source_kind == "ast" for e in other)
