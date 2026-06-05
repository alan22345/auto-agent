"""Tests for the ``cycles_for`` op on ``query_repo_graph`` (ADR-016 Phase 9).

Five cases:
  1. Blob with one cycle containing members [a, b]: cycles_for(a) returns that cycle.
  2. Same blob: cycles_for(b) also returns that cycle.
  3. cycles_for(not_in_any_cycle) returns empty list (no error).
  4. Blob with TWO cycles where only one contains the queried id → returns just that one.
  5. cycles_for appears in _KNOWN_OPS and is NOT rejected as unknown.

Shares the same DB-stub/workspace approach as test_query_repo_graph_tool.py.
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
    DependencyCycle,
    EdgeEvidence,
    RepoGraphBlob,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _ev(file: str = "a.py", line: int = 1) -> EdgeEvidence:
    return EdgeEvidence(file=file, line=line, snippet="import x")


def _make_cycle(
    cycle_id: str,
    kind: str = "import",
    members: list[str] | None = None,
) -> DependencyCycle:
    members = members or ["module:a", "module:b"]
    return DependencyCycle(
        id=cycle_id,
        kind=kind,
        members=members,
        closing_edges=[_ev()],
    )


def _make_blob(cycles: list[DependencyCycle] | None = None) -> RepoGraphBlob:
    """Minimal blob with no nodes/edges — cycles_for only needs blob.cycles."""
    return RepoGraphBlob(
        commit_sha="cycle-test-sha",
        generated_at=datetime.now(UTC),
        analyser_version="phase9-tests-0.0.1",
        areas=[AreaStatus(name="area_a", status="ok")],
        nodes=[],
        edges=[],
        public_symbols=[],
        cycles=cycles or [],
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


def _make_config(
    *, repo_id: int = 1, workspace_path: str = "/ws", last_analysis_id: int | None = 42
):
    cfg = MagicMock()
    cfg.repo_id = repo_id
    cfg.workspace_path = workspace_path
    cfg.last_analysis_id = last_analysis_id
    return cfg


def _make_graph_row(
    *, graph_id: int = 42, commit_sha: str = "cycle-test-sha", blob: RepoGraphBlob | None = None
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


def _fake_staleness(graph_sha: str = "cycle-test-sha") -> Staleness:
    return Staleness(graph_sha=graph_sha, workspace_sha=graph_sha, drifted=False)


async def _run(tool: QueryRepoGraphTool, tmp_path: Path, node_id: str, blob: RepoGraphBlob):
    """Run the cycles_for op with a fixed blob, patching DB + staleness."""
    cfg = _make_config(workspace_path=str(tmp_path))
    graph_row = _make_graph_row(blob=blob)
    session_factory = lambda: _SessionStub(config=cfg, graph=graph_row)  # noqa: E731
    fake = _fake_staleness()

    with (
        patch("agent.tools.query_repo_graph.async_session", new=session_factory),
        patch("agent.tools.query_repo_graph.compute_staleness", return_value=fake),
    ):
        return await tool.execute(
            {"repo_id": 1, "op": "cycles_for", "params": {"node_id": node_id}},
            _make_context(str(tmp_path)),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycles_for_returns_cycle_containing_first_member(tmp_path: Path) -> None:
    """cycles_for(a) returns the cycle whose members list contains a."""
    cycle = _make_cycle("cyc-1", members=["module:a", "module:b"])
    blob = _make_blob(cycles=[cycle])

    tool = QueryRepoGraphTool()
    result = await _run(tool, tmp_path, "module:a", blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    assert payload["op"] == "cycles_for"
    assert "staleness" in payload

    cycles = payload["result"]
    assert len(cycles) == 1
    assert cycles[0]["id"] == "cyc-1"
    assert "module:a" in cycles[0]["members"]
    assert "module:b" in cycles[0]["members"]
    assert len(cycles[0]["closing_edges"]) == 1


@pytest.mark.asyncio
async def test_cycles_for_returns_cycle_containing_second_member(tmp_path: Path) -> None:
    """cycles_for(b) also returns the same cycle."""
    cycle = _make_cycle("cyc-1", members=["module:a", "module:b"])
    blob = _make_blob(cycles=[cycle])

    tool = QueryRepoGraphTool()
    result = await _run(tool, tmp_path, "module:b", blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    cycles = payload["result"]
    assert len(cycles) == 1
    assert cycles[0]["id"] == "cyc-1"


@pytest.mark.asyncio
async def test_cycles_for_returns_empty_list_for_id_in_no_cycle(tmp_path: Path) -> None:
    """A node id not present in any cycle member list → empty list, no error."""
    cycle = _make_cycle("cyc-1", members=["module:a", "module:b"])
    blob = _make_blob(cycles=[cycle])

    tool = QueryRepoGraphTool()
    result = await _run(tool, tmp_path, "module:not_in_any_cycle", blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    assert payload["result"] == []


@pytest.mark.asyncio
async def test_cycles_for_returns_only_matching_cycle_when_two_exist(tmp_path: Path) -> None:
    """With two cycles, only the one whose members contain the queried id is returned."""
    cycle_ab = _make_cycle("cyc-ab", members=["module:a", "module:b"])
    cycle_cd = _make_cycle("cyc-cd", members=["module:c", "module:d"])
    blob = _make_blob(cycles=[cycle_ab, cycle_cd])

    tool = QueryRepoGraphTool()
    result = await _run(tool, tmp_path, "module:c", blob)

    assert result.is_error is False
    payload = json.loads(result.output)
    cycles = payload["result"]
    assert len(cycles) == 1
    assert cycles[0]["id"] == "cyc-cd"


def test_cycles_for_is_in_known_ops() -> None:
    """cycles_for must appear in _KNOWN_OPS so it is not rejected."""
    assert "cycles_for" in _KNOWN_OPS


@pytest.mark.asyncio
async def test_cycles_for_not_rejected_as_unknown_op(tmp_path: Path) -> None:
    """The tool must NOT return an unknown-op error for cycles_for."""
    blob = _make_blob(cycles=[])
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
            {"repo_id": 1, "op": "cycles_for", "params": {"node_id": "module:x"}},
            _make_context(str(tmp_path)),
        )

    assert result.is_error is False
    assert "unknown op" not in result.output.lower()
