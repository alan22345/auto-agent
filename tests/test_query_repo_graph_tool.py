"""Tests for the ``query_repo_graph`` agent tool (ADR-016 Phase 6 §12).

The tool exposes seven read-only ops over the latest stored graph
analysis for a repo, wraps every response in a staleness envelope, and
flags whether each node/edge's referenced file still exists in the
analyser workspace.

Strategy:
* Build a small in-memory :class:`shared.types.RepoGraphBlob` covering
  every op's interesting cases (cross-area edges, public/private
  symbols, a multi-hop path, a violating edge).
* Patch the DB loader (``async_session``) so we don't need real
  Postgres; one stub returns the config + graph row, another returns
  ``last_analysis_id=None`` for the no-graph case.
* Patch ``compute_staleness`` to return predictable SHAs.
* Use a real tmp_path workspace so the existence check exercises the
  actual ``os.path.exists`` call (one file present, one missing).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — used as runtime annotation by pytest fixtures
from unittest.mock import MagicMock, patch

import pytest

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
# Fixture blob — small, hand-built graph
# ---------------------------------------------------------------------------


def _make_blob(*, public_symbols: list[str] | None = None) -> RepoGraphBlob:
    """Build a minimal fixture graph.

    Topology:
        area_a/                          area_b/
          mod_a.py::PublicA                 mod_b.py::PublicB
            -> mod_b.py::PublicB                -> mod_c.py::HelperC (cross-area, public surface)
            -> mod_b.py::_private              (private surface — internal_access)
          mod_a.py::caller
            -> mod_a.py::PublicA

        area_c/
          mod_c.py::HelperC
            -> mod_d.py::Leaf

    "missing.py" appears as a node, lets us assert
    ``exists_in_workspace=False`` for symbols the agent's branch deleted.
    """
    public = (
        public_symbols
        if public_symbols is not None
        else [
            "area_a/mod_a.py::PublicA",
            "area_a/mod_a.py::caller",
            "area_b/mod_b.py::PublicB",
            "area_c/mod_c.py::HelperC",
            "area_c/mod_d.py::Leaf",
        ]
    )

    nodes = [
        Node(
            id="area:area_a",
            kind="area",
            label="area_a",
            area="area_a",
        ),
        Node(
            id="area_a/mod_a.py::PublicA",
            kind="function",
            label="PublicA",
            file="area_a/mod_a.py",
            area="area_a",
            parent="area:area_a",
        ),
        Node(
            id="area_a/mod_a.py::caller",
            kind="function",
            label="caller",
            file="area_a/mod_a.py",
            area="area_a",
            parent="area:area_a",
        ),
        Node(
            id="area_a/missing.py::Gone",
            kind="function",
            label="Gone",
            file="area_a/missing.py",
            area="area_a",
            parent="area:area_a",
        ),
        Node(
            id="area:area_b",
            kind="area",
            label="area_b",
            area="area_b",
        ),
        Node(
            id="area_b/mod_b.py::PublicB",
            kind="function",
            label="PublicB",
            file="area_b/mod_b.py",
            area="area_b",
            parent="area:area_b",
        ),
        Node(
            id="area_b/mod_b.py::_private",
            kind="function",
            label="_private",
            file="area_b/mod_b.py",
            area="area_b",
            parent="area:area_b",
        ),
        Node(
            id="area:area_c",
            kind="area",
            label="area_c",
            area="area_c",
        ),
        Node(
            id="area_c/mod_c.py::HelperC",
            kind="function",
            label="HelperC",
            file="area_c/mod_c.py",
            area="area_c",
            parent="area:area_c",
        ),
        Node(
            id="area_c/mod_d.py::Leaf",
            kind="function",
            label="Leaf",
            file="area_c/mod_d.py",
            area="area_c",
            parent="area:area_c",
        ),
    ]

    def _ev(file: str, line: int = 1, snippet: str = "x") -> EdgeEvidence:
        return EdgeEvidence(file=file, line=line, snippet=snippet)

    edges = [
        Edge(
            source="area_a/mod_a.py::caller",
            target="area_a/mod_a.py::PublicA",
            kind="calls",
            evidence=_ev("area_a/mod_a.py", 2, "PublicA()"),
            source_kind="ast",
        ),
        Edge(
            source="area_a/mod_a.py::PublicA",
            target="area_b/mod_b.py::PublicB",
            kind="calls",
            evidence=_ev("area_a/mod_a.py", 5, "PublicB()"),
            source_kind="ast",
        ),
        Edge(
            source="area_a/mod_a.py::PublicA",
            target="area_b/mod_b.py::_private",
            kind="calls",
            evidence=_ev("area_a/mod_a.py", 6, "_private()"),
            source_kind="ast",
            boundary_violation=True,
            violation_reason="internal_access",
        ),
        Edge(
            source="area_b/mod_b.py::PublicB",
            target="area_c/mod_c.py::HelperC",
            kind="calls",
            evidence=_ev("area_b/mod_b.py", 3, "HelperC()"),
            source_kind="ast",
        ),
        Edge(
            source="area_c/mod_c.py::HelperC",
            target="area_c/mod_d.py::Leaf",
            kind="calls",
            evidence=_ev("area_c/mod_c.py", 2, "Leaf()"),
            source_kind="ast",
        ),
    ]

    return RepoGraphBlob(
        commit_sha="graph-sha-1",
        generated_at=datetime.now(UTC),
        analyser_version="phase6-tests-0.0.1",
        areas=[
            AreaStatus(name="area_a", status="ok"),
            AreaStatus(name="area_b", status="ok"),
            AreaStatus(name="area_c", status="ok"),
        ],
        nodes=nodes,
        edges=edges,
        public_symbols=public,
    )


# ---------------------------------------------------------------------------
# DB stubs
# ---------------------------------------------------------------------------


class _SessionStub:
    """Mimics ``async with async_session() as s: await s.execute(...)``.

    The tool's only DB interactions are:
      1. Look up the ``RepoGraphConfig`` row for ``repo_id`` and read
         ``last_analysis_id`` + ``workspace_path``.
      2. If non-null, load the ``RepoGraph`` row by that id and read
         ``commit_sha`` + ``graph_json``.

    We answer both by class:  ``execute(select(RepoGraphConfig).where...)``
    returns the configured config; ``execute(select(RepoGraph).where...)``
    returns the configured graph.
    """

    def __init__(
        self,
        *,
        config: object | None,
        graph: object | None,
    ) -> None:
        self._config = config
        self._graph = graph

    async def __aenter__(self) -> _SessionStub:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, stmt: object) -> object:
        # Inspect the select target — works for SQLAlchemy 2.x.
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


def _make_config(
    *, repo_id: int = 1, workspace_path: str = "/ws", last_analysis_id: int | None = 42
):
    cfg = MagicMock()
    cfg.repo_id = repo_id
    cfg.workspace_path = workspace_path
    cfg.last_analysis_id = last_analysis_id
    return cfg


def _make_graph_row(
    *, graph_id: int = 42, commit_sha: str = "graph-sha-1", blob: RepoGraphBlob | None = None
):
    row = MagicMock()
    row.id = graph_id
    row.commit_sha = commit_sha
    row.graph_json = (blob or _make_blob()).model_dump(mode="json")
    return row


def _make_context(workspace: str) -> ToolContext:
    return ToolContext(workspace=workspace)


def _setup_workspace(tmp_path: Path) -> Path:
    """Create a fake workspace with the fixture files that should "exist"."""
    ws = tmp_path / "graph-ws"
    for rel in [
        "area_a/mod_a.py",
        "area_b/mod_b.py",
        "area_c/mod_c.py",
        "area_c/mod_d.py",
        # NOTE: area_a/missing.py deliberately omitted.
    ]:
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# fixture\n")
    return ws


# ---------------------------------------------------------------------------
# Tool plumbing — no graph / errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_config_returns_error_result(tmp_path: Path) -> None:
    tool = QueryRepoGraphTool()
    session_factory = lambda: _SessionStub(config=None, graph=None)  # noqa: E731

    with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
        result = await tool.execute(
            {"repo_id": 99, "op": "callers_of", "params": {"node_id": "x"}},
            _make_context(str(tmp_path)),
        )

    assert result.is_error is True
    assert "No graph available for repo 99" in result.output


@pytest.mark.asyncio
async def test_config_without_analysis_returns_error_result(tmp_path: Path) -> None:
    tool = QueryRepoGraphTool()
    cfg = _make_config(repo_id=7, last_analysis_id=None)
    session_factory = lambda: _SessionStub(config=cfg, graph=None)  # noqa: E731

    with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
        result = await tool.execute(
            {"repo_id": 7, "op": "callers_of", "params": {"node_id": "x"}},
            _make_context(str(tmp_path)),
        )

    assert result.is_error is True
    assert "No graph available for repo 7" in result.output


@pytest.mark.asyncio
async def test_unknown_op_returns_error_result(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path)
    cfg = _make_config(workspace_path=str(ws))
    graph_row = _make_graph_row()
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731
    tool = QueryRepoGraphTool()

    with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
        result = await tool.execute(
            {"repo_id": 1, "op": "obliterate_universe", "params": {}},
            _make_context(str(ws)),
        )
    assert result.is_error is True
    assert "unknown op" in result.output.lower()


@pytest.mark.asyncio
async def test_missing_op_returns_error_result(tmp_path: Path) -> None:
    tool = QueryRepoGraphTool()
    cfg = _make_config(workspace_path=str(tmp_path))
    graph_row = _make_graph_row()
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

    with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
        result = await tool.execute(
            {"repo_id": 1, "params": {}},
            _make_context(str(tmp_path)),
        )
    assert result.is_error is True


# ---------------------------------------------------------------------------
# Envelope shape — staleness on every response
# ---------------------------------------------------------------------------


def _run_tool_sync(
    tool,
    ws: Path,
    op: str,
    params: dict,
    *,
    graph_sha: str = "graph-sha-1",
    workspace_sha: str | None = "graph-sha-1",
    drifted: bool = False,
):
    """Async runner shortcut — patches DB + staleness deterministically."""
    import asyncio

    cfg = _make_config(workspace_path=str(ws))
    graph_row = _make_graph_row(commit_sha=graph_sha)
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731
    fake_staleness = Staleness(
        graph_sha=graph_sha,
        workspace_sha=workspace_sha,
        drifted=drifted,
    )
    with (
        patch("agent.tools.query_repo_graph.async_session", new=session_factory),
        patch(
            "agent.tools.query_repo_graph.compute_staleness",
            return_value=fake_staleness,
        ),
    ):
        return asyncio.get_event_loop().run_until_complete(
            tool.execute(
                {"repo_id": 1, "op": op, "params": params},
                _make_context(str(ws)),
            ),
        )


@pytest.mark.asyncio
async def test_envelope_includes_staleness_dict_on_every_response(
    tmp_path: Path,
) -> None:
    ws = _setup_workspace(tmp_path)
    cfg = _make_config(workspace_path=str(ws))
    graph_row = _make_graph_row()
    fake = Staleness(graph_sha="g", workspace_sha="w", drifted=True)
    tool = QueryRepoGraphTool()

    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731
    with (
        patch("agent.tools.query_repo_graph.async_session", new=session_factory),
        patch(
            "agent.tools.query_repo_graph.compute_staleness",
            return_value=fake,
        ),
    ):
        result = await tool.execute(
            {
                "repo_id": 1,
                "op": "callers_of",
                "params": {"node_id": "area_a/mod_a.py::PublicA"},
            },
            _make_context(str(ws)),
        )

    assert result.is_error is False
    payload = json.loads(result.output)
    assert "staleness" in payload
    assert payload["staleness"] == {
        "graph_sha": "g",
        "workspace_sha": "w",
        "drifted": True,
    }


# ---------------------------------------------------------------------------
# Per-op tests
# ---------------------------------------------------------------------------


class TestCallersOf:
    @pytest.mark.asyncio
    async def test_returns_nodes_with_edge_pointing_at_target(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "callers_of",
                    "params": {"node_id": "area_a/mod_a.py::PublicA"},
                },
                _make_context(str(ws)),
            )

        payload = json.loads(result.output)
        # `result` contains the list of caller node ids/labels;
        # `results_with_existence` wraps each with existence flag.
        ids = [item["value"]["id"] for item in payload["results_with_existence"]]
        assert ids == ["area_a/mod_a.py::caller"]
        # Caller's file exists in the fake workspace.
        assert payload["results_with_existence"][0]["exists_in_workspace"] is True

    @pytest.mark.asyncio
    async def test_existence_false_when_file_missing(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        # Add an edge to Gone (which is in area_a/missing.py — not on disk).
        blob = _make_blob()
        blob.edges.append(
            Edge(
                source="area_a/missing.py::Gone",
                target="area_a/mod_a.py::PublicA",
                kind="calls",
                evidence=EdgeEvidence(
                    file="area_a/missing.py",
                    line=1,
                    snippet="x",
                ),
                source_kind="ast",
            ),
        )
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row(blob=blob)
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "callers_of",
                    "params": {"node_id": "area_a/mod_a.py::PublicA"},
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        by_id = {
            item["value"]["id"]: item["exists_in_workspace"]
            for item in payload["results_with_existence"]
        }
        assert by_id["area_a/mod_a.py::caller"] is True
        assert by_id["area_a/missing.py::Gone"] is False


class TestCalleesOf:
    @pytest.mark.asyncio
    async def test_returns_nodes_called_by_source(self, tmp_path: Path) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "callees_of",
                    "params": {"node_id": "area_a/mod_a.py::PublicA"},
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        ids = {item["value"]["id"] for item in payload["results_with_existence"]}
        assert ids == {
            "area_b/mod_b.py::PublicB",
            "area_b/mod_b.py::_private",
        }


class TestOutgoingEdges:
    @pytest.mark.asyncio
    async def test_returns_raw_edge_objects_with_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "outgoing_edges",
                    "params": {"node_id": "area_a/mod_a.py::PublicA"},
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        items = payload["results_with_existence"]
        assert len(items) == 2
        for item in items:
            edge = item["value"]
            assert edge["source"] == "area_a/mod_a.py::PublicA"
            assert "evidence" in edge
            assert "source_kind" in edge


class TestIncomingEdges:
    @pytest.mark.asyncio
    async def test_returns_raw_edge_objects_targeting_node(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "incoming_edges",
                    "params": {"node_id": "area_c/mod_c.py::HelperC"},
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        items = payload["results_with_existence"]
        assert len(items) == 1
        assert items[0]["value"]["source"] == "area_b/mod_b.py::PublicB"
        assert items[0]["value"]["target"] == "area_c/mod_c.py::HelperC"


class TestPublicSurface:
    @pytest.mark.asyncio
    async def test_returns_nodes_in_area_that_are_publicly_exposed(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "public_surface",
                    "params": {"area_name": "area_b"},
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        ids = {item["value"]["id"] for item in payload["results_with_existence"]}
        # area_b public surface = PublicB (in public_symbols + area_b); not _private.
        assert ids == {"area_b/mod_b.py::PublicB"}

    @pytest.mark.asyncio
    async def test_unknown_area_returns_empty_list(self, tmp_path: Path) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "public_surface",
                    "params": {"area_name": "nowhere"},
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        assert payload["results_with_existence"] == []


class TestPathBetween:
    @pytest.mark.asyncio
    async def test_returns_node_id_path_when_one_exists(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "path_between",
                    "params": {
                        "source_id": "area_a/mod_a.py::PublicA",
                        "target_id": "area_c/mod_d.py::Leaf",
                    },
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        # BFS shortest path: PublicA -> PublicB -> HelperC -> Leaf
        assert payload["result"] == [
            "area_a/mod_a.py::PublicA",
            "area_b/mod_b.py::PublicB",
            "area_c/mod_c.py::HelperC",
            "area_c/mod_d.py::Leaf",
        ]
        # No results_with_existence for path payloads (single-value result).
        assert "results_with_existence" not in payload

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_path_exists(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "path_between",
                    "params": {
                        # No edge from Leaf back upstream.
                        "source_id": "area_c/mod_d.py::Leaf",
                        "target_id": "area_a/mod_a.py::PublicA",
                    },
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        assert payload["result"] == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_path_exceeds_max_depth(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "path_between",
                    "params": {
                        "source_id": "area_a/mod_a.py::PublicA",
                        "target_id": "area_c/mod_d.py::Leaf",
                        "max_depth": 2,  # Real path is 3 hops; cap at 2.
                    },
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        assert payload["result"] == []

    @pytest.mark.asyncio
    async def test_same_source_and_target_returns_single_element_path(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "path_between",
                    "params": {
                        "source_id": "area_a/mod_a.py::PublicA",
                        "target_id": "area_a/mod_a.py::PublicA",
                    },
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        assert payload["result"] == ["area_a/mod_a.py::PublicA"]


class TestViolatesBoundaries:
    @pytest.mark.asyncio
    async def test_returns_edge_dict_with_violation_when_one_exists(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "violates_boundaries",
                    "params": {
                        "source_id": "area_a/mod_a.py::PublicA",
                        "target_id": "area_b/mod_b.py::_private",
                    },
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        edge = payload["result"]
        assert edge is not None
        assert edge["boundary_violation"] is True
        assert edge["violation_reason"] == "internal_access"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_edge_exists(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "violates_boundaries",
                    "params": {
                        "source_id": "area_c/mod_d.py::Leaf",
                        "target_id": "area_a/mod_a.py::PublicA",
                    },
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        assert payload["result"] is None

    @pytest.mark.asyncio
    async def test_returns_edge_dict_with_no_violation_when_clean(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        cfg = _make_config(workspace_path=str(ws))
        graph_row = _make_graph_row()
        tool = QueryRepoGraphTool()
        session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

        with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
            result = await tool.execute(
                {
                    "repo_id": 1,
                    "op": "violates_boundaries",
                    "params": {
                        "source_id": "area_a/mod_a.py::PublicA",
                        "target_id": "area_b/mod_b.py::PublicB",
                    },
                },
                _make_context(str(ws)),
            )
        payload = json.loads(result.output)
        edge = payload["result"]
        assert edge is not None
        assert edge["boundary_violation"] is False
        assert edge["violation_reason"] is None


# ---------------------------------------------------------------------------
# Existence flagging — verifying the workspace file check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existence_check_uses_analyser_workspace_from_config(
    tmp_path: Path,
) -> None:
    """The tool checks file existence against the config's
    ``workspace_path`` — NOT the agent's ``ToolContext.workspace``. The
    config is authoritative for "where was the graph analysed against"."""

    analyser_ws = tmp_path / "analyser"
    analyser_ws.mkdir()
    # Only mod_a.py exists in the analyser workspace.
    (analyser_ws / "area_a").mkdir()
    (analyser_ws / "area_a" / "mod_a.py").write_text("# .\n")

    # Tool context points at a totally different (empty) directory.
    agent_ws = tmp_path / "agent"
    agent_ws.mkdir()

    cfg = _make_config(workspace_path=str(analyser_ws))
    graph_row = _make_graph_row()
    tool = QueryRepoGraphTool()
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

    with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
        result = await tool.execute(
            {
                "repo_id": 1,
                "op": "callees_of",
                "params": {"node_id": "area_a/mod_a.py::PublicA"},
            },
            _make_context(str(agent_ws)),
        )
    payload = json.loads(result.output)
    by_id = {
        item["value"]["id"]: item["exists_in_workspace"]
        for item in payload["results_with_existence"]
    }
    # Both PublicB and _private point at area_b/mod_b.py — not on disk.
    assert by_id["area_b/mod_b.py::PublicB"] is False
    assert by_id["area_b/mod_b.py::_private"] is False


@pytest.mark.asyncio
async def test_node_without_file_field_is_marked_existing(
    tmp_path: Path,
) -> None:
    """Area-kind nodes have ``file=None``; treat them as existing."""
    ws = _setup_workspace(tmp_path)
    blob = _make_blob()
    # Add an edge that points at the area:area_b node so callers_of returns it.
    blob.edges.append(
        Edge(
            source="area_a/mod_a.py::caller",
            target="area:area_b",
            kind="calls",
            evidence=EdgeEvidence(
                file="area_a/mod_a.py",
                line=99,
                snippet="x",
            ),
            source_kind="ast",
        ),
    )
    cfg = _make_config(workspace_path=str(ws))
    graph_row = _make_graph_row(blob=blob)
    tool = QueryRepoGraphTool()
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

    with patch("agent.tools.query_repo_graph.async_session", new=session_factory):
        result = await tool.execute(
            {
                "repo_id": 1,
                "op": "callees_of",
                "params": {"node_id": "area_a/mod_a.py::caller"},
            },
            _make_context(str(ws)),
        )
    payload = json.loads(result.output)
    by_id = {
        item["value"]["id"]: item["exists_in_workspace"]
        for item in payload["results_with_existence"]
    }
    assert by_id["area:area_b"] is True


# ---------------------------------------------------------------------------
# Tool schema sanity
# ---------------------------------------------------------------------------


def test_tool_is_read_only_and_has_required_params() -> None:
    tool = QueryRepoGraphTool()
    assert tool.is_readonly is True
    assert tool.name == "query_repo_graph"
    required = tool.parameters["required"]
    assert "repo_id" in required
    assert "op" in required
