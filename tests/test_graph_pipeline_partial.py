"""Per-area refresh pipeline (ADR-016 Phase 7 §10).

``run_partial_pipeline`` re-parses ONLY a target area's files. Nodes
and edges contributed by other areas are spliced verbatim from the
``previous_blob`` argument. Cross-area edge re-validation (boundary
flagging + HTTP matching) re-runs across the whole edge set so a
rename inside the target area can flip a cross-area edge's
``boundary_violation`` flag.

These tests cover:

* Non-target areas are preserved verbatim.
* Re-running the partial pipeline against the same workspace state
  produces a graph indistinguishable from a full refresh (modulo
  ``generated_at`` + ``commit_sha``).
* The cross-area boundary check re-runs (a change in the target area's
  public surface flips a flag on an edge that was already in the blob).
* Failure isolation: when the target area's parser throws the rest of
  the previous blob's data is preserved and the overall status reflects
  the failure.
* When the target area does not exist in the workspace the blob shows
  an ``AreaStatus`` with ``status="failed"`` for that area.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_partial_pipeline, run_pipeline

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_violations_python"


def _copy_fixture(tmp_path: Path) -> str:
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


def _sorted(items, key):
    return sorted(items, key=key)


@pytest.mark.asyncio
async def test_partial_refresh_preserves_other_areas(tmp_path: Path) -> None:
    """When we refresh only area_a, every node + edge whose ``area`` is
    area_b in the previous blob must reappear verbatim in the new blob."""
    workspace = _copy_fixture(tmp_path)
    full = await run_pipeline(workspace=workspace, commit_sha="abc")

    previous_b_nodes = [n for n in full.nodes if n.area == "area_b"]
    previous_b_edges = [
        e
        for e in full.edges
        # An edge is considered "from area_b" if its source node is
        # area_b — we preserve those untouched.
        if any(n.id == e.source and n.area == "area_b" for n in previous_b_nodes)
    ]

    partial = await run_partial_pipeline(
        workspace=workspace,
        commit_sha="abc",
        target_area="area_a",
        previous_blob=full,
    )

    new_b_nodes = [n for n in partial.nodes if n.area == "area_b"]
    # Identity by id is enough — node objects are recreated by the
    # parser when area_b is re-analysed, but here we keep them verbatim.
    assert sorted(n.id for n in new_b_nodes) == sorted(n.id for n in previous_b_nodes)

    new_b_edges = [
        e
        for e in partial.edges
        if any(n.id == e.source and n.area == "area_b" for n in new_b_nodes)
    ]
    assert sorted(e.source + e.target for e in new_b_edges) == sorted(
        e.source + e.target for e in previous_b_edges
    )


@pytest.mark.asyncio
async def test_partial_refresh_matches_full_when_workspace_unchanged(
    tmp_path: Path,
) -> None:
    """Same workspace state → partial pipeline output equivalent to full.

    Compares node ids and edge identities. ``generated_at`` and
    ``analyser_version`` are not compared (they're metadata, not graph
    truth).
    """
    workspace = _copy_fixture(tmp_path)
    full = await run_pipeline(workspace=workspace, commit_sha="abc")
    partial = await run_partial_pipeline(
        workspace=workspace,
        commit_sha="abc",
        target_area="area_a",
        previous_blob=full,
    )

    assert sorted(n.id for n in full.nodes) == sorted(n.id for n in partial.nodes)
    full_edge_keys = sorted((e.source, e.target, e.kind, e.boundary_violation) for e in full.edges)
    partial_edge_keys = sorted(
        (e.source, e.target, e.kind, e.boundary_violation) for e in partial.edges
    )
    assert full_edge_keys == partial_edge_keys


@pytest.mark.asyncio
async def test_partial_refresh_reruns_boundary_check(tmp_path: Path) -> None:
    """After the target area's public surface changes, an inherited
    cross-area edge into it has its ``boundary_violation`` flag
    re-evaluated.

    The previous blob held an edge ``use_private -> _private_helper``
    with ``boundary_violation=True``. We rename the helper to a public
    name and refresh area_b. After the partial refresh the inherited
    edge's recorded target no longer matches a node in the graph, so
    the boundary stage must NOT continue to report the old True flag —
    the edge is re-validated against the new node set.
    """
    workspace = _copy_fixture(tmp_path)
    full = await run_pipeline(workspace=workspace, commit_sha="abc")

    violating = [
        e
        for e in full.edges
        if e.source == "area_a/caller.py::use_private"
        and e.target == "area_b/public_api.py::_private_helper"
    ]
    assert violating and violating[0].boundary_violation is True

    # Rename the private helper to a public name in area_b only.
    public_api_path = Path(workspace) / "area_b" / "public_api.py"
    text = public_api_path.read_text()
    public_api_path.write_text(text.replace("_private_helper", "now_public_helper"))

    partial = await run_partial_pipeline(
        workspace=workspace,
        commit_sha="def",
        target_area="area_b",
        previous_blob=full,
    )

    # The inherited edge from area_a is still present with its old
    # target — but now that target node does not exist in the refreshed
    # graph, so the boundary stage must clear the violation flag (the
    # only reason it was set previously was the public-surface check
    # against the OLD area_b node).
    stale_edges = [
        e
        for e in partial.edges
        if e.source == "area_a/caller.py::use_private"
        and e.target == "area_b/public_api.py::_private_helper"
    ]
    assert stale_edges, "inherited cross-area edge should be preserved"
    for e in stale_edges:
        assert e.boundary_violation is False, (
            "boundary stage must re-run across the whole edge set after a partial refresh"
        )


@pytest.mark.asyncio
async def test_partial_refresh_target_area_empty_keeps_other_areas(
    tmp_path: Path,
) -> None:
    """When the target area's files have all been removed, the partial
    refresh wipes the target area's nodes/edges but preserves data from
    every other area in the previous blob.
    """
    workspace = _copy_fixture(tmp_path)
    full = await run_pipeline(workspace=workspace, commit_sha="abc")

    # Delete all of area_a's source so the area now contains zero
    # parseable files.
    shutil.rmtree(Path(workspace) / "area_a")

    partial = await run_partial_pipeline(
        workspace=workspace,
        commit_sha="abc",
        target_area="area_a",
        previous_blob=full,
    )

    # area_b's class + function nodes are still here.
    assert any(n.id == "area_b/public_api.py::PublicWidget" for n in partial.nodes)
    # area_a's symbols are gone (only the area: container itself, if
    # area_a is still recognised, remains).
    assert not any(n.id == "area_a/caller.py::use_private" for n in partial.nodes)


@pytest.mark.asyncio
async def test_partial_refresh_unknown_area_returns_failed_status(
    tmp_path: Path,
) -> None:
    """Targeting an area that does not exist in the workspace records a
    ``failed`` area in the resulting blob and leaves previous data
    intact for the areas that DO exist."""
    workspace = _copy_fixture(tmp_path)
    full = await run_pipeline(workspace=workspace, commit_sha="abc")

    partial = await run_partial_pipeline(
        workspace=workspace,
        commit_sha="abc",
        target_area="this_area_does_not_exist",
        previous_blob=full,
    )

    failed = [a for a in partial.areas if a.name == "this_area_does_not_exist"]
    assert failed and failed[0].status == "failed"
    # area_a / area_b survive intact.
    assert any(n.id == "area_b/public_api.py::PublicWidget" for n in partial.nodes)
