"""Tests for quality read ops on ``query_repo_graph`` (ADR-016 Phase 9 §7 Task A).

Five new ops:
  - hotspots: returns blob.hotspots truncated to limit.
  - clones: returns blob.clones filtered to token_len >= min_tokens.
  - dead_code: returns blob.dead_code optionally filtered by kind.
  - complex_functions: returns function nodes where metric >= threshold.
  - file_health: returns blob.file_health optionally filtered by band.

Uses the same DB-stub/workspace pattern as test_query_repo_graph_cycles.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from unittest.mock import MagicMock, patch

import pytest

from agent.graph_analyzer.staleness import Staleness
from agent.tools.base import ToolContext
from agent.tools.query_repo_graph import _KNOWN_OPS, QueryRepoGraphTool
from shared.types import (
    AreaStatus,
    CloneGroup,
    CloneInstance,
    DeadCodeFinding,
    EdgeEvidence,
    FileHealth,
    Hotspot,
    Node,
    RepoGraphBlob,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_node(
    node_id: str,
    kind: str = "function",
    cyclomatic: int | None = None,
    cognitive: int | None = None,
) -> Node:
    return Node(
        id=node_id,
        kind=kind,  # type: ignore[arg-type]
        label=node_id,
        area="area_a",
        cyclomatic=cyclomatic,
        cognitive=cognitive,
    )


def _make_hotspot(file: str, score: float) -> Hotspot:
    return Hotspot(
        file=file,
        churn=1.0,
        complexity_density=0.5,
        score=score,
        trend="stable",
    )


def _make_clone_group(group_id: str, token_len: int) -> CloneGroup:
    return CloneGroup(
        id=group_id,
        token_len=token_len,
        mode="strict",
        instances=[
            CloneInstance(node_id="n1", file="a.py", line_start=1, line_end=5),
            CloneInstance(node_id="n2", file="b.py", line_start=10, line_end=14),
        ],
    )


def _make_dead_code(kind: str, target: str) -> DeadCodeFinding:
    return DeadCodeFinding(
        kind=kind,  # type: ignore[arg-type]
        target=target,
        reason="unused",
    )


def _make_file_health(file: str, band: str) -> FileHealth:
    mi = {"good": 85.0, "moderate": 55.0, "poor": 20.0}[band]
    return FileHealth(file=file, maintainability_index=mi, band=band)  # type: ignore[arg-type]


def _ev(file: str = "a.py", line: int = 1) -> EdgeEvidence:
    return EdgeEvidence(file=file, line=line, snippet="import x")


def _make_blob(
    *,
    nodes: list[Node] | None = None,
    hotspots: list[Hotspot] | None = None,
    clones: list[CloneGroup] | None = None,
    dead_code: list[DeadCodeFinding] | None = None,
    file_health: list[FileHealth] | None = None,
) -> RepoGraphBlob:
    return RepoGraphBlob(
        commit_sha="quality-test-sha",
        generated_at=datetime.now(UTC),
        analyser_version="quality-tests-0.0.1",
        areas=[AreaStatus(name="area_a", status="ok")],
        nodes=nodes or [],
        edges=[],
        public_symbols=[],
        cycles=[],
        hotspots=hotspots or [],
        clones=clones or [],
        dead_code=dead_code or [],
        file_health=file_health or [],
    )


# ---------------------------------------------------------------------------
# DB stubs — same pattern as test_query_repo_graph_cycles.py
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
    commit_sha: str = "quality-test-sha",
    blob: RepoGraphBlob | None = None,
):
    row = MagicMock()
    row.id = graph_id
    row.commit_sha = commit_sha
    b = blob or _make_blob()
    row.graph_json = b.model_dump(mode="json")
    row.flow_json = None
    return row


def _make_context(workspace: str) -> ToolContext:
    return ToolContext(workspace=workspace)


def _fake_staleness(graph_sha: str = "quality-test-sha") -> Staleness:
    return Staleness(graph_sha=graph_sha, workspace_sha=graph_sha, drifted=False)


async def _run(
    tool: QueryRepoGraphTool,
    tmp_path: Path,
    op: str,
    params: dict,
    blob: RepoGraphBlob,
):
    """Invoke ``op`` over a fixed blob, patching DB + staleness."""
    cfg = _make_config(workspace_path=str(tmp_path))
    graph_row = _make_graph_row(blob=blob)
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731
    fake = _fake_staleness()

    with (
        patch("agent.tools.query_repo_graph.async_session", new=session_factory),
        patch("agent.tools.query_repo_graph.compute_staleness", return_value=fake),
    ):
        return await tool.execute(
            {"repo_id": 1, "op": op, "params": params},
            _make_context(str(tmp_path)),
        )


# ---------------------------------------------------------------------------
# hotspots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hotspots_returns_all_hotspots(tmp_path: Path) -> None:
    """hotspots with no limit returns all hotspots from the blob."""
    hs = [_make_hotspot("a.py", 90.0), _make_hotspot("b.py", 70.0)]
    blob = _make_blob(hotspots=hs)

    result = await _run(QueryRepoGraphTool(), tmp_path, "hotspots", {}, blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    assert payload["op"] == "hotspots"
    assert "staleness" in payload
    items = payload["result"]
    assert len(items) == 2
    assert items[0]["file"] == "a.py"
    assert items[1]["file"] == "b.py"


@pytest.mark.asyncio
async def test_hotspots_limit_truncates_to_top_n(tmp_path: Path) -> None:
    """hotspots with limit=1 returns only the first (highest-scored) hotspot."""
    hs = [_make_hotspot("a.py", 90.0), _make_hotspot("b.py", 70.0)]
    blob = _make_blob(hotspots=hs)

    result = await _run(QueryRepoGraphTool(), tmp_path, "hotspots", {"limit": 1}, blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    items = payload["result"]
    assert len(items) == 1
    assert items[0]["file"] == "a.py"


# ---------------------------------------------------------------------------
# clones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clones_returns_all_clone_groups(tmp_path: Path) -> None:
    """clones with no min_tokens returns all clone groups."""
    groups = [_make_clone_group("g1", 50), _make_clone_group("g2", 20)]
    blob = _make_blob(clones=groups)

    result = await _run(QueryRepoGraphTool(), tmp_path, "clones", {}, blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    assert payload["op"] == "clones"
    assert "staleness" in payload
    items = payload["result"]
    assert len(items) == 2


@pytest.mark.asyncio
async def test_clones_min_tokens_filters_out_small_groups(tmp_path: Path) -> None:
    """clones with min_tokens=30 excludes groups with token_len < 30."""
    groups = [_make_clone_group("g1", 50), _make_clone_group("g2", 20)]
    blob = _make_blob(clones=groups)

    result = await _run(QueryRepoGraphTool(), tmp_path, "clones", {"min_tokens": 30}, blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    items = payload["result"]
    assert len(items) == 1
    assert items[0]["id"] == "g1"


# ---------------------------------------------------------------------------
# dead_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_code_returns_all_findings(tmp_path: Path) -> None:
    """dead_code with no kind filter returns all findings."""
    findings = [
        _make_dead_code("unused_export", "mod:foo"),
        _make_dead_code("unused_file", "file:bar.py"),
    ]
    blob = _make_blob(dead_code=findings)

    result = await _run(QueryRepoGraphTool(), tmp_path, "dead_code", {}, blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    assert payload["op"] == "dead_code"
    assert "staleness" in payload
    items = payload["result"]
    assert len(items) == 2


@pytest.mark.asyncio
async def test_dead_code_kind_filter(tmp_path: Path) -> None:
    """dead_code with kind='unused_export' filters to only that kind."""
    findings = [
        _make_dead_code("unused_export", "mod:foo"),
        _make_dead_code("unused_file", "file:bar.py"),
    ]
    blob = _make_blob(dead_code=findings)

    result = await _run(
        QueryRepoGraphTool(), tmp_path, "dead_code", {"kind": "unused_export"}, blob
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    items = payload["result"]
    assert len(items) == 1
    assert items[0]["kind"] == "unused_export"


@pytest.mark.asyncio
async def test_dead_code_invalid_kind_returns_error(tmp_path: Path) -> None:
    """dead_code with an invalid kind value returns an error result."""
    blob = _make_blob()

    result = await _run(QueryRepoGraphTool(), tmp_path, "dead_code", {"kind": "not_a_kind"}, blob)

    assert result.is_error is True
    assert "not_a_kind" in result.output or "invalid" in result.output.lower()


# ---------------------------------------------------------------------------
# complex_functions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complex_functions_cyclomatic_threshold(tmp_path: Path) -> None:
    """complex_functions returns function nodes with cyclomatic >= threshold."""
    nodes = [
        _make_node("fn:a", cyclomatic=15),
        _make_node("fn:b", cyclomatic=5),
        _make_node("fn:c", cyclomatic=10),
    ]
    blob = _make_blob(nodes=nodes)

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "complex_functions",
        {"metric": "cyclomatic", "threshold": 10},
        blob,
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    assert payload["op"] == "complex_functions"
    assert "staleness" in payload
    items = payload["result"]
    assert len(items) == 2
    # sorted desc by cyclomatic then id asc
    assert items[0]["id"] == "fn:a"
    assert items[1]["id"] == "fn:c"


@pytest.mark.asyncio
async def test_complex_functions_excludes_none_metric(tmp_path: Path) -> None:
    """complex_functions excludes nodes where the metric is None."""
    nodes = [
        _make_node("fn:a", cyclomatic=15),
        _make_node("fn:no_metric", cyclomatic=None),
    ]
    blob = _make_blob(nodes=nodes)

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "complex_functions",
        {"metric": "cyclomatic", "threshold": 1},
        blob,
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    items = payload["result"]
    node_ids = [n["id"] for n in items]
    assert "fn:no_metric" not in node_ids
    assert "fn:a" in node_ids


@pytest.mark.asyncio
async def test_complex_functions_cognitive_metric(tmp_path: Path) -> None:
    """complex_functions works with metric='cognitive'."""
    nodes = [
        _make_node("fn:high", cognitive=20),
        _make_node("fn:low", cognitive=3),
    ]
    blob = _make_blob(nodes=nodes)

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "complex_functions",
        {"metric": "cognitive", "threshold": 10},
        blob,
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    items = payload["result"]
    assert len(items) == 1
    assert items[0]["id"] == "fn:high"


@pytest.mark.asyncio
async def test_complex_functions_invalid_metric_returns_error(tmp_path: Path) -> None:
    """complex_functions with an invalid metric name returns an error."""
    blob = _make_blob()

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "complex_functions",
        {"metric": "not_a_metric", "threshold": 5},
        blob,
    )

    assert result.is_error is True


@pytest.mark.asyncio
async def test_complex_functions_missing_metric_returns_error(tmp_path: Path) -> None:
    """complex_functions without metric param returns an error."""
    blob = _make_blob()

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "complex_functions",
        {"threshold": 5},
        blob,
    )

    assert result.is_error is True


@pytest.mark.asyncio
async def test_complex_functions_missing_threshold_returns_error(tmp_path: Path) -> None:
    """complex_functions without threshold param returns an error."""
    blob = _make_blob()

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "complex_functions",
        {"metric": "cyclomatic"},
        blob,
    )

    assert result.is_error is True


# ---------------------------------------------------------------------------
# file_health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_health_returns_all_entries(tmp_path: Path) -> None:
    """file_health with no band filter returns all entries."""
    entries = [
        _make_file_health("a.py", "good"),
        _make_file_health("b.py", "poor"),
    ]
    blob = _make_blob(file_health=entries)

    result = await _run(QueryRepoGraphTool(), tmp_path, "file_health", {}, blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    assert payload["op"] == "file_health"
    assert "staleness" in payload
    items = payload["result"]
    assert len(items) == 2


@pytest.mark.asyncio
async def test_file_health_band_filter(tmp_path: Path) -> None:
    """file_health with band='poor' returns only poor entries."""
    entries = [
        _make_file_health("a.py", "good"),
        _make_file_health("b.py", "poor"),
        _make_file_health("c.py", "moderate"),
    ]
    blob = _make_blob(file_health=entries)

    result = await _run(QueryRepoGraphTool(), tmp_path, "file_health", {"band": "poor"}, blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    items = payload["result"]
    assert len(items) == 1
    assert items[0]["file"] == "b.py"
    assert items[0]["band"] == "poor"


@pytest.mark.asyncio
async def test_file_health_invalid_band_returns_error(tmp_path: Path) -> None:
    """file_health with an invalid band value returns an error."""
    blob = _make_blob()

    result = await _run(QueryRepoGraphTool(), tmp_path, "file_health", {"band": "not_a_band"}, blob)

    assert result.is_error is True


# ---------------------------------------------------------------------------
# _KNOWN_OPS membership + unknown op rejection
# ---------------------------------------------------------------------------


def test_all_new_ops_in_known_ops() -> None:
    """Every new quality op must appear in _KNOWN_OPS."""
    for op in ("hotspots", "clones", "dead_code", "complex_functions", "file_health"):
        assert op in _KNOWN_OPS, f"'{op}' missing from _KNOWN_OPS"


@pytest.mark.asyncio
async def test_unknown_op_still_rejected(tmp_path: Path) -> None:
    """A completely unknown op name must still be rejected as unknown."""
    blob = _make_blob()
    cfg = _make_config(workspace_path=str(tmp_path))
    graph_row = _make_graph_row(blob=blob)
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731
    fake = _fake_staleness()

    tool = QueryRepoGraphTool()
    with (
        patch("agent.tools.query_repo_graph.async_session", new=session_factory),
        patch("agent.tools.query_repo_graph.compute_staleness", return_value=fake),
    ):
        result = await tool.execute(
            {"repo_id": 1, "op": "totally_unknown_op", "params": {}},
            _make_context(str(tmp_path)),
        )

    assert result.is_error is True
    assert "unknown op" in result.output.lower()


# ---------------------------------------------------------------------------
# Staleness envelope shape for each new op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hotspots_result_in_staleness_envelope(tmp_path: Path) -> None:
    """hotspots result is embedded inside the staleness envelope."""
    blob = _make_blob(hotspots=[_make_hotspot("x.py", 50.0)])

    result = await _run(QueryRepoGraphTool(), tmp_path, "hotspots", {}, blob)
    payload = json.loads(result.output)

    assert "op" in payload
    assert "staleness" in payload
    assert "result" in payload
    assert isinstance(payload["staleness"]["graph_sha"], str)
    assert isinstance(payload["staleness"]["drifted"], bool)


@pytest.mark.asyncio
async def test_clones_result_in_staleness_envelope(tmp_path: Path) -> None:
    """clones result is embedded inside the staleness envelope."""
    blob = _make_blob(clones=[_make_clone_group("g1", 10)])

    result = await _run(QueryRepoGraphTool(), tmp_path, "clones", {}, blob)
    payload = json.loads(result.output)

    assert "op" in payload
    assert "staleness" in payload
    assert "result" in payload


@pytest.mark.asyncio
async def test_dead_code_result_in_staleness_envelope(tmp_path: Path) -> None:
    """dead_code result is embedded inside the staleness envelope."""
    blob = _make_blob(dead_code=[_make_dead_code("unused_export", "mod:x")])

    result = await _run(QueryRepoGraphTool(), tmp_path, "dead_code", {}, blob)
    payload = json.loads(result.output)

    assert "op" in payload
    assert "staleness" in payload
    assert "result" in payload


@pytest.mark.asyncio
async def test_complex_functions_result_in_staleness_envelope(tmp_path: Path) -> None:
    """complex_functions result is embedded inside the staleness envelope."""
    blob = _make_blob(nodes=[_make_node("fn:x", cyclomatic=5)])

    result = await _run(
        QueryRepoGraphTool(),
        tmp_path,
        "complex_functions",
        {"metric": "cyclomatic", "threshold": 1},
        blob,
    )
    payload = json.loads(result.output)

    assert "op" in payload
    assert "staleness" in payload
    assert "result" in payload


@pytest.mark.asyncio
async def test_file_health_result_in_staleness_envelope(tmp_path: Path) -> None:
    """file_health result is embedded inside the staleness envelope."""
    blob = _make_blob(file_health=[_make_file_health("x.py", "good")])

    result = await _run(QueryRepoGraphTool(), tmp_path, "file_health", {}, blob)
    payload = json.loads(result.output)

    assert "op" in payload
    assert "staleness" in payload
    assert "result" in payload
