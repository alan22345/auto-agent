"""End-to-end Phase 5 pipeline tests (ADR-016 Phase 5 §7).

The pipeline runs the boundary-flagging stage after HTTP matching. With
the new ``graph_repo_violations_python`` fixture we exercise both the
internal-access and explicit-rule paths, and verify HTTP edges remain
unflagged.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_violations_python"
_CROSSLANG_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_crosslang"


def _setup(tmp_path: Path, fixture: Path) -> str:
    target = tmp_path / "workspace"
    shutil.copytree(fixture, target)
    return str(target)


def _write_graph_yml(workspace: str, text: str) -> None:
    auto = Path(workspace) / ".auto-agent"
    auto.mkdir(exist_ok=True)
    (auto / "graph.yml").write_text(text)


@pytest.mark.asyncio
async def test_internal_access_violation_is_flagged(tmp_path: Path) -> None:
    ws = _setup(tmp_path, _FIXTURE)
    blob = await run_pipeline(workspace=ws, commit_sha="abc")

    # Find the call to _private_helper.
    private_edge = next(
        (
            e
            for e in blob.edges
            if e.source == "area_a/caller.py::use_private"
            and e.target == "area_b/public_api.py::_private_helper"
        ),
        None,
    )
    assert private_edge is not None
    assert private_edge.boundary_violation is True
    assert private_edge.violation_reason == "internal_access"


@pytest.mark.asyncio
async def test_clean_cross_area_edge_is_not_flagged(tmp_path: Path) -> None:
    ws = _setup(tmp_path, _FIXTURE)
    blob = await run_pipeline(workspace=ws, commit_sha="abc")

    # PublicWidget() in use_public — collapsed to a call to the class.
    public_edge = next(
        (
            e
            for e in blob.edges
            if e.source == "area_a/caller.py::use_public"
            and e.target == "area_b/public_api.py::PublicWidget"
        ),
        None,
    )
    assert public_edge is not None
    assert public_edge.boundary_violation is False
    assert public_edge.violation_reason is None


@pytest.mark.asyncio
async def test_absent_yaml_means_no_explicit_rules(tmp_path: Path) -> None:
    ws = _setup(tmp_path, _FIXTURE)
    # No .auto-agent/graph.yml present in the fixture — confirm none of
    # the violations report an explicit_rule reason.
    blob = await run_pipeline(workspace=ws, commit_sha="abc")
    for e in blob.edges:
        if e.violation_reason:
            assert e.violation_reason == "internal_access"


@pytest.mark.asyncio
async def test_explicit_rule_flags_clean_cross_area_edge(tmp_path: Path) -> None:
    ws = _setup(tmp_path, _FIXTURE)
    _write_graph_yml(
        ws,
        "boundaries:\n  - forbid:\n      from: area_a\n      to: [area_b]\n",
    )
    blob = await run_pipeline(workspace=ws, commit_sha="abc")
    public_edge = next(
        e
        for e in blob.edges
        if e.source == "area_a/caller.py::use_public"
        and e.target == "area_b/public_api.py::PublicWidget"
    )
    # Public target, but the explicit rule forbids ANY edge from area_a
    # → area_b — so flagged.
    assert public_edge.boundary_violation is True
    assert public_edge.violation_reason == "explicit_rule:0"


@pytest.mark.asyncio
async def test_explicit_rule_wins_over_internal_access(tmp_path: Path) -> None:
    ws = _setup(tmp_path, _FIXTURE)
    _write_graph_yml(
        ws,
        "boundaries:\n  - forbid:\n      from: area_a\n      to: [area_b]\n",
    )
    blob = await run_pipeline(workspace=ws, commit_sha="abc")
    private_edge = next(
        e
        for e in blob.edges
        if e.source == "area_a/caller.py::use_private"
        and e.target == "area_b/public_api.py::_private_helper"
    )
    # Both checks would fire — explicit rule takes precedence.
    assert private_edge.boundary_violation is True
    assert private_edge.violation_reason == "explicit_rule:0"


@pytest.mark.asyncio
async def test_http_edges_are_never_flagged(tmp_path: Path) -> None:
    ws = _setup(tmp_path, _CROSSLANG_FIXTURE)
    _write_graph_yml(
        ws,
        # Even with an explicit rule that would otherwise match the
        # cross-language HTTP edges, http kind is unconditionally exempt.
        "boundaries:\n  - forbid:\n      from: web_next_area\n      to: [orchestrator_area]\n",
    )
    blob = await run_pipeline(workspace=ws, commit_sha="abc")
    http_edges = [e for e in blob.edges if e.kind == "http"]
    assert http_edges, "fixture should produce at least one http edge"
    for e in http_edges:
        assert e.boundary_violation is False
        assert e.violation_reason is None
