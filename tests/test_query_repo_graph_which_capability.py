"""Tests for the ``which_capability`` op on ``query_repo_graph``.

Four cases:
  1. Node appears in a flow → returns flows + capability, unreached=False.
  2. Node in graph but not on any flow → unreached=True, flows=[], capability=None.
  3. Node not in graph at all → {"error": "node_not_found"}.
  4. RepoGraph row has flow_json=null (not yet computed) → unreached=True + note.

Shares the same DB-stub/workspace approach as test_query_repo_graph_tool.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from unittest.mock import MagicMock, patch

import pytest

from agent.graph_analyzer.flows import derive_flow_blob
from agent.graph_analyzer.staleness import Staleness
from agent.tools.base import ToolContext
from agent.tools.query_repo_graph import QueryRepoGraphTool
from shared.types import (
    AreaStatus,
    Edge,
    EdgeEvidence,
    Node,
    RepoGraphBlob,
)

# ---------------------------------------------------------------------------
# Fixture graph — minimal topology that exercises which_capability
#
# Topology (function nodes only; area nodes not used for flow derivation):
#   entry_a  ->  middle_a  ->  leaf_a        (one flow: entry_a)
#   isolated                                  (no flow — unreached)
#
# derive_flow_blob will produce:
#   flows:        [flow for entry_a covering entry_a, middle_a, leaf_a]
#   capabilities: [Capability(id="unlabeled", flow_ids=[<entry_a flow id>])]
#   unreached:    ["module_a.py::isolated"]
# ---------------------------------------------------------------------------


def _make_blob() -> RepoGraphBlob:
    nodes = [
        Node(
            id="area:area_a",
            kind="area",
            label="area_a",
            area="area_a",
        ),
        Node(
            id="module_a.py::entry_a",
            kind="function",
            label="entry_a",
            file="module_a.py",
            area="area_a",
            parent="area:area_a",
        ),
        Node(
            id="module_a.py::middle_a",
            kind="function",
            label="middle_a",
            file="module_a.py",
            area="area_a",
            parent="area:area_a",
        ),
        Node(
            id="module_a.py::leaf_a",
            kind="function",
            label="leaf_a",
            file="module_a.py",
            area="area_a",
            parent="area:area_a",
        ),
        Node(
            id="module_a.py::isolated",
            kind="function",
            label="isolated",
            file="module_a.py",
            area="area_a",
            parent="area:area_a",
        ),
    ]

    def _ev(line: int = 1) -> EdgeEvidence:
        return EdgeEvidence(file="module_a.py", line=line, snippet="x()")

    edges = [
        # HTTP edge makes entry_a a detected entry point.
        Edge(
            source="router",
            target="module_a.py::entry_a",
            kind="http",
            evidence=_ev(1),
            source_kind="ast",
        ),
        Edge(
            source="module_a.py::entry_a",
            target="module_a.py::middle_a",
            kind="calls",
            evidence=_ev(2),
            source_kind="ast",
        ),
        Edge(
            source="module_a.py::middle_a",
            target="module_a.py::leaf_a",
            kind="calls",
            evidence=_ev(3),
            source_kind="ast",
        ),
    ]

    return RepoGraphBlob(
        commit_sha="test-sha-wc",
        generated_at=datetime.now(UTC),
        analyser_version="wc-tests-0.0.1",
        areas=[AreaStatus(name="area_a", status="ok")],
        nodes=nodes,
        edges=edges,
        public_symbols=[
            "module_a.py::entry_a",
            "module_a.py::middle_a",
            "module_a.py::leaf_a",
        ],
    )


# ---------------------------------------------------------------------------
# DB stubs — copied from test_query_repo_graph_tool.py pattern
# ---------------------------------------------------------------------------


class _SessionStub:
    def __init__(self, *, config: object | None, graph: object | None) -> None:
        self._config = config
        self._graph = graph

    async def __aenter__(self) -> _SessionStub:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, stmt: object) -> object:
        from shared.models import RepoGraph, RepoGraphConfig

        target = None
        try:
            cols = stmt.column_descriptions
            target = cols[0]["entity"] if cols else None
        except Exception:
            target = None

        result = MagicMock()
        if target is RepoGraphConfig:
            result.scalar_one_or_none.return_value = self._config
        elif target is RepoGraph:
            result.scalar_one_or_none.return_value = self._graph
        else:
            result.scalar_one_or_none.return_value = None
        return result


def _make_config(*, repo_id: int = 1, workspace_path: str = "/ws", last_analysis_id: int | None = 42):
    cfg = MagicMock()
    cfg.repo_id = repo_id
    cfg.workspace_path = workspace_path
    cfg.last_analysis_id = last_analysis_id
    return cfg


def _make_graph_row(*, graph_id: int = 42, commit_sha: str = "test-sha-wc", flow_json=None, blob=None):
    row = MagicMock()
    row.id = graph_id
    row.commit_sha = commit_sha
    b = blob or _make_blob()
    row.graph_json = b.model_dump(mode="json")
    row.flow_json = flow_json
    return row


def _make_context(workspace: str) -> ToolContext:
    return ToolContext(workspace=workspace)


def _fake_staleness(graph_sha: str = "test-sha-wc") -> Staleness:
    return Staleness(graph_sha=graph_sha, workspace_sha=graph_sha, drifted=False)


async def _run(tool, ws: str, params: dict, *, flow_json=None, blob=None):
    """Run the which_capability op, patching DB + staleness."""
    cfg = _make_config(workspace_path=ws)
    graph_row = _make_graph_row(flow_json=flow_json, blob=blob)
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731
    fake = _fake_staleness()

    with (
        patch("agent.tools.query_repo_graph.async_session", new=session_factory),
        patch("agent.tools.query_repo_graph.compute_staleness", return_value=fake),
    ):
        return await tool.execute(
            {"repo_id": 1, "op": "which_capability", "params": params},
            _make_context(ws),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_which_capability_returns_flow_for_reached_node(tmp_path: Path) -> None:
    """A node that appears in a flow's steps returns flows + capability."""
    blob = _make_blob()
    flow_blob = derive_flow_blob(blob, workspace_root=None)
    flow_json = flow_blob.model_dump(mode="json")

    tool = QueryRepoGraphTool()
    result = await _run(tool, str(tmp_path), {"node": "module_a.py::middle_a"}, flow_json=flow_json)

    assert result.is_error is False
    payload = json.loads(result.output)
    assert payload["op"] == "which_capability"
    assert "staleness" in payload

    data = payload["result"]
    assert data["unreached"] is False
    assert len(data["flows"]) >= 1
    # Every returned flow must have the required keys.
    for f in data["flows"]:
        assert "id" in f
        assert "name" in f  # Phase 1 → None, but key must exist
        assert "entry_point_node_id" in f
        assert "terminal_kind" in f
    # Capability must be returned with id="unlabeled" and name=None in Phase 1.
    cap = data["capability"]
    assert cap is not None
    assert cap["id"] == "unlabeled"
    assert cap["name"] is None


@pytest.mark.asyncio
async def test_which_capability_reports_unreached(tmp_path: Path) -> None:
    """A node in the graph but touched by no flow returns unreached=True."""
    blob = _make_blob()
    flow_blob = derive_flow_blob(blob, workspace_root=None)
    flow_json = flow_blob.model_dump(mode="json")

    tool = QueryRepoGraphTool()
    result = await _run(tool, str(tmp_path), {"node": "module_a.py::isolated"}, flow_json=flow_json)

    assert result.is_error is False
    payload = json.loads(result.output)
    data = payload["result"]
    assert data["unreached"] is True
    assert data["flows"] == []
    assert data["capability"] is None
    assert "note" not in data  # note only appears when flow_json is null


@pytest.mark.asyncio
async def test_which_capability_node_not_in_graph(tmp_path: Path) -> None:
    """A node id that is not in the graph at all returns node_not_found."""
    blob = _make_blob()
    flow_blob = derive_flow_blob(blob, workspace_root=None)
    flow_json = flow_blob.model_dump(mode="json")

    tool = QueryRepoGraphTool()
    result = await _run(
        tool, str(tmp_path), {"node": "nonexistent.py::ghost"}, flow_json=flow_json
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    data = payload["result"]
    assert data == {"error": "node_not_found"}


@pytest.mark.asyncio
async def test_which_capability_when_flow_json_is_null(tmp_path: Path) -> None:
    """When flow_json is null the op returns unreached=True with an actionable note."""
    tool = QueryRepoGraphTool()
    result = await _run(
        tool, str(tmp_path), {"node": "module_a.py::entry_a"}, flow_json=None
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    data = payload["result"]
    assert data["unreached"] is True
    assert data["flows"] == []
    assert data["capability"] is None
    assert "note" in data
    assert "recompute" in data["note"]
