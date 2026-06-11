"""Tests for the navigation ops on ``query_repo_graph`` (ADR-023).

Two new ops that make the graph usable as the agent's entry/exit points
for code navigation, so finding a symbol no longer needs grep and
reading one costs a function body, not a whole file:

  - search_symbols: find nodes by name — case-insensitive, ranked
    exact > prefix > substring on label, then id substring; optional
    kind/area filters; existence-wrapped like other node-returning ops.
  - get_symbol_source: return the clamped source window for one node,
    read from the analyser workspace (same caps as the side-panel
    code-preview endpoint).

Uses the same DB-stub/workspace pattern as test_query_repo_graph_quality.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from unittest.mock import MagicMock, patch

import pytest

from agent.graph_analyzer.staleness import Staleness
from agent.tools.base import ToolContext
from agent.tools.query_repo_graph import QueryRepoGraphTool
from shared.types import (
    AreaStatus,
    Node,
    RepoGraphBlob,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_node(
    node_id: str,
    *,
    kind: str = "function",
    label: str | None = None,
    file: str | None = None,
    area: str = "area_a",
    line_start: int | None = None,
    line_end: int | None = None,
) -> Node:
    return Node(
        id=node_id,
        kind=kind,  # type: ignore[arg-type]
        label=label if label is not None else node_id.rsplit("::", 1)[-1],
        file=file,
        area=area,
        line_start=line_start,
        line_end=line_end,
    )


def _make_blob(nodes: list[Node]) -> RepoGraphBlob:
    return RepoGraphBlob(
        commit_sha="search-test-sha",
        generated_at=datetime.now(UTC),
        analyser_version="search-tests-0.0.1",
        areas=[
            AreaStatus(name="area_a", status="ok"),
            AreaStatus(name="area_b", status="ok"),
        ],
        nodes=nodes,
        edges=[],
        public_symbols=[],
    )


def _search_fixture_nodes() -> list[Node]:
    """Nodes covering every ranking tier for query "parse".

    - parse              exact label match        (tier 0)
    - parse_config       label prefix             (tier 1)
    - try_reparse        label substring          (tier 2)
    - helper (in parsers/util.py)  id-only match  (tier 3)
    - unrelated          no match
    """
    return [
        _make_node(
            "area_a/x.py::parse",
            file="area_a/x.py",
            line_start=1,
            line_end=2,
        ),
        _make_node(
            "area_a/x.py::parse_config",
            file="area_a/x.py",
            line_start=4,
            line_end=6,
        ),
        _make_node(
            "area_b/y.py::try_reparse",
            area="area_b",
            file="area_b/y.py",
            line_start=1,
            line_end=3,
        ),
        _make_node(
            "area_a/parsers/util.py::helper",
            file="area_a/parsers/util.py",
            line_start=1,
            line_end=2,
        ),
        _make_node(
            "area_a/x.py::unrelated",
            file="area_a/x.py",
            line_start=8,
            line_end=9,
        ),
        _make_node(
            "area_a/x.py::ParseError",
            kind="class",
            file="area_a/x.py",
            line_start=11,
            line_end=12,
        ),
    ]


# ---------------------------------------------------------------------------
# DB stubs — same pattern as test_query_repo_graph_quality.py
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


def _make_config(
    *, repo_id: int = 1, workspace_path: str = "/ws", last_analysis_id: int | None = 42
):
    cfg = MagicMock()
    cfg.repo_id = repo_id
    cfg.workspace_path = workspace_path
    cfg.last_analysis_id = last_analysis_id
    return cfg


def _make_graph_row(
    *,
    graph_id: int = 42,
    commit_sha: str = "search-test-sha",
    blob: RepoGraphBlob | None = None,
):
    row = MagicMock()
    row.id = graph_id
    row.commit_sha = commit_sha
    b = blob or _make_blob(_search_fixture_nodes())
    row.graph_json = b.model_dump(mode="json")
    row.flow_json = None
    return row


def _fake_staleness(graph_sha: str = "search-test-sha") -> Staleness:
    return Staleness(graph_sha=graph_sha, workspace_sha=graph_sha, drifted=False)


async def _run(
    tool: QueryRepoGraphTool,
    workspace: Path,
    op: str,
    params: dict,
    blob: RepoGraphBlob,
):
    """Invoke ``op`` over a fixed blob, patching DB + staleness."""
    cfg = _make_config(workspace_path=str(workspace))
    graph_row = _make_graph_row(blob=blob)
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731
    fake = _fake_staleness()

    with (
        patch("agent.tools.query_repo_graph.async_session", new=session_factory),
        patch("agent.tools.query_repo_graph.compute_staleness", return_value=fake),
    ):
        return await tool.execute(
            {"repo_id": 1, "op": op, "params": params},
            ToolContext(workspace=str(workspace)),
        )


def _result_ids(result) -> list[str]:
    payload = json.loads(result.output)
    return [n["id"] for n in payload["result"]]


# ---------------------------------------------------------------------------
# search_symbols — ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_ranks_exact_before_prefix_before_substring(tmp_path: Path) -> None:
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(QueryRepoGraphTool(), tmp_path, "search_symbols", {"query": "parse"}, blob)

    assert result.is_error is False
    ids = _result_ids(result)
    assert ids[0] == "area_a/x.py::parse"
    assert ids[1] == "area_a/x.py::parse_config"
    assert "area_b/y.py::try_reparse" in ids
    assert "area_a/x.py::unrelated" not in ids


@pytest.mark.asyncio
async def test_search_is_case_insensitive(tmp_path: Path) -> None:
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(QueryRepoGraphTool(), tmp_path, "search_symbols", {"query": "PARSE"}, blob)

    assert result.is_error is False
    assert _result_ids(result)[0] == "area_a/x.py::parse"


@pytest.mark.asyncio
async def test_search_matches_on_id_when_label_does_not(tmp_path: Path) -> None:
    """A node whose label doesn't match but whose id (file path) does is found."""
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(
        QueryRepoGraphTool(), tmp_path, "search_symbols", {"query": "parsers/util"}, blob
    )

    assert result.is_error is False
    assert _result_ids(result) == ["area_a/parsers/util.py::helper"]


@pytest.mark.asyncio
async def test_search_id_matches_rank_after_label_matches(tmp_path: Path) -> None:
    """ "parse" matches parsers/util.py::helper by id only — it ranks last."""
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(QueryRepoGraphTool(), tmp_path, "search_symbols", {"query": "parse"}, blob)

    ids = _result_ids(result)
    assert "area_a/parsers/util.py::helper" in ids
    label_matches = [i for i in ids if i != "area_a/parsers/util.py::helper"]
    assert ids.index("area_a/parsers/util.py::helper") == len(label_matches)


# ---------------------------------------------------------------------------
# search_symbols — filters + limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_kind_filter(tmp_path: Path) -> None:
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "search_symbols",
        {"query": "parse", "kind": "class"},
        blob,
    )

    assert _result_ids(result) == ["area_a/x.py::ParseError"]


@pytest.mark.asyncio
async def test_search_area_filter(tmp_path: Path) -> None:
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "search_symbols",
        {"query": "parse", "area": "area_b"},
        blob,
    )

    assert _result_ids(result) == ["area_b/y.py::try_reparse"]


@pytest.mark.asyncio
async def test_search_limit_truncates(tmp_path: Path) -> None:
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "search_symbols",
        {"query": "parse", "limit": 1},
        blob,
    )

    assert _result_ids(result) == ["area_a/x.py::parse"]


@pytest.mark.asyncio
async def test_search_results_carry_existence_flags(tmp_path: Path) -> None:
    """Node-returning op → results_with_existence present, missing file → False."""
    ws = tmp_path / "ws"
    (ws / "area_a").mkdir(parents=True)
    (ws / "area_a" / "x.py").write_text("# present\n")
    # area_a/parsers/util.py and area_b/y.py deliberately absent.
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(QueryRepoGraphTool(), ws, "search_symbols", {"query": "parse"}, blob)

    payload = json.loads(result.output)
    by_id = {
        item["value"]["id"]: item["exists_in_workspace"]
        for item in payload["results_with_existence"]
    }
    assert by_id["area_a/x.py::parse"] is True
    assert by_id["area_b/y.py::try_reparse"] is False


@pytest.mark.asyncio
async def test_search_missing_query_is_error(tmp_path: Path) -> None:
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(QueryRepoGraphTool(), tmp_path, "search_symbols", {}, blob)

    assert result.is_error is True
    assert "query" in result.output


@pytest.mark.asyncio
async def test_search_bad_kind_is_error(tmp_path: Path) -> None:
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "search_symbols",
        {"query": "parse", "kind": "banana"},
        blob,
    )

    assert result.is_error is True
    assert "kind" in result.output


# ---------------------------------------------------------------------------
# get_symbol_source
# ---------------------------------------------------------------------------

_SRC = "def parse():\n    return 1\n\ndef parse_config():\n    x = 2\n    return x\n"


def _source_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "area_a").mkdir(parents=True)
    (ws / "area_a" / "x.py").write_text(_SRC)
    return ws


@pytest.mark.asyncio
async def test_get_symbol_source_returns_exact_window(tmp_path: Path) -> None:
    ws = _source_workspace(tmp_path)
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(
        QueryRepoGraphTool(),
        ws,
        "get_symbol_source",
        {"node_id": "area_a/x.py::parse_config"},
        blob,
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    body = payload["result"]
    assert body["node_id"] == "area_a/x.py::parse_config"
    assert body["file"] == "area_a/x.py"
    assert body["line_start"] == 4
    assert body["line_end"] == 6
    assert body["source"] == "def parse_config():\n    x = 2\n    return x\n"
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_get_symbol_source_context_lines_extend_window(tmp_path: Path) -> None:
    ws = _source_workspace(tmp_path)
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(
        QueryRepoGraphTool(),
        ws,
        "get_symbol_source",
        {"node_id": "area_a/x.py::parse", "context_lines": 2},
        blob,
    )

    payload = json.loads(result.output)
    body = payload["result"]
    # Node is lines 1-2; context clamps at file start, extends below.
    assert body["line_start"] == 1
    assert body["line_end"] == 4
    assert body["source"] == "def parse():\n    return 1\n\ndef parse_config():\n"


@pytest.mark.asyncio
async def test_get_symbol_source_unknown_node_is_error(tmp_path: Path) -> None:
    ws = _source_workspace(tmp_path)
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(
        QueryRepoGraphTool(),
        ws,
        "get_symbol_source",
        {"node_id": "area_a/x.py::nope"},
        blob,
    )

    assert result.is_error is True
    assert "area_a/x.py::nope" in result.output


@pytest.mark.asyncio
async def test_get_symbol_source_area_node_is_error(tmp_path: Path) -> None:
    ws = _source_workspace(tmp_path)
    nodes = [*_search_fixture_nodes(), _make_node("area:area_a", kind="area", label="area_a")]
    blob = _make_blob(nodes)

    result = await _run(
        QueryRepoGraphTool(),
        ws,
        "get_symbol_source",
        {"node_id": "area:area_a"},
        blob,
    )

    assert result.is_error is True
    assert "no source file" in result.output


@pytest.mark.asyncio
async def test_get_symbol_source_missing_file_mentions_staleness(tmp_path: Path) -> None:
    ws = _source_workspace(tmp_path)
    blob = _make_blob(_search_fixture_nodes())

    result = await _run(
        QueryRepoGraphTool(),
        ws,
        "get_symbol_source",
        {"node_id": "area_b/y.py::try_reparse"},  # file never written
        blob,
    )

    assert result.is_error is True
    assert "stale" in result.output.lower()


@pytest.mark.asyncio
async def test_get_symbol_source_clamps_oversize_windows(tmp_path: Path) -> None:
    """A node spanning more lines than the cap returns a truncated window."""
    ws = tmp_path / "ws"
    (ws / "area_a").mkdir(parents=True)
    (ws / "area_a" / "big.py").write_text("".join(f"line{i}\n" for i in range(1, 701)))
    nodes = [
        _make_node(
            "area_a/big.py::mega",
            file="area_a/big.py",
            line_start=1,
            line_end=700,
        ),
    ]
    blob = _make_blob(nodes)

    result = await _run(
        QueryRepoGraphTool(),
        ws,
        "get_symbol_source",
        {"node_id": "area_a/big.py::mega"},
        blob,
    )

    assert result.is_error is False
    body = json.loads(result.output)["result"]
    assert body["truncated"] is True
    assert body["line_end"] == 500
    assert body["source"].splitlines()[-1] == "line500"


@pytest.mark.asyncio
async def test_get_symbol_source_traversal_node_is_rejected(tmp_path: Path) -> None:
    """A node whose file escapes the workspace is refused (defence in depth)."""
    ws = _source_workspace(tmp_path)
    (tmp_path / "secret.txt").write_text("nope\n")
    nodes = [
        _make_node(
            "../secret.txt::evil",
            file="../secret.txt",
            line_start=1,
            line_end=1,
        ),
    ]
    blob = _make_blob(nodes)

    result = await _run(
        QueryRepoGraphTool(),
        ws,
        "get_symbol_source",
        {"node_id": "../secret.txt::evil"},
        blob,
    )

    assert result.is_error is True
    assert "nope" not in result.output


@pytest.mark.asyncio
async def test_staleness_is_computed_against_the_analysis_branch(tmp_path: Path) -> None:
    """ADR-024: the tool must hand cfg.analysis_branch to compute_staleness
    so drift is measured against origin, not just the frozen workspace."""
    cfg = _make_config(workspace_path=str(tmp_path))
    cfg.analysis_branch = "develop"
    graph_row = _make_graph_row(blob=_make_blob(_search_fixture_nodes()))
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731

    with (
        patch("agent.tools.query_repo_graph.async_session", new=session_factory),
        patch(
            "agent.tools.query_repo_graph.compute_staleness",
            return_value=_fake_staleness(),
        ) as staleness,
    ):
        await QueryRepoGraphTool().execute(
            {"repo_id": 1, "op": "hotspots", "params": {}},
            ToolContext(workspace=str(tmp_path)),
        )

    assert staleness.call_args.kwargs["analysis_branch"] == "develop"
