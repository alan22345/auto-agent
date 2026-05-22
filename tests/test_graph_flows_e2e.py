"""End-to-end smoke: run the graph pipeline on the python fixture, then
derive flows, and assert the result has the expected shape.

This is a smoke test, not a comprehensive correctness test — the
per-module unit tests in test_graph_entry_points / test_graph_flows_*
cover behaviour. This one catches integration drift across the chain:
parser → blob → entry_points → trace → terminal → derive → FlowJsonBlob.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.flows import DERIVER_VERSION, derive_flow_blob
from agent.graph_analyzer.pipeline import run_pipeline

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_python"


def _setup_workspace(tmp_path: Path) -> Path:
    """Copy the python fixture into tmp_path so derive_flow_blob can
    read its files when hashing file_set contents."""
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return target


@pytest.mark.asyncio
async def test_derive_against_python_fixture(tmp_path: Path) -> None:
    ws = _setup_workspace(tmp_path)
    graph_blob = await run_pipeline(workspace=str(ws), commit_sha="abc1234")
    flow_blob = derive_flow_blob(graph_blob, workspace_root=ws)

    # Phase 1 always emits one capability (the "unlabeled" placeholder).
    assert len(flow_blob.capabilities) == 1
    assert flow_blob.capabilities[0].id == "unlabeled"

    # Commit SHA and deriver version propagate.
    assert flow_blob.derived_at_commit == graph_blob.commit_sha
    assert flow_blob.deriver_version == DERIVER_VERSION

    # Either we found flows, or we found nothing reachable — but the
    # blob is never inconsistent: every step node id refers to an
    # actual node in the graph.
    node_ids = {n.id for n in graph_blob.nodes}
    for flow in flow_blob.flows:
        for step in flow.steps:
            assert step.node_id in node_ids
        assert flow.entry_point.node_id in node_ids
        assert flow.terminal_node_id in node_ids

    # Every unreached id is also a real node id.
    for nid in flow_blob.unreached:
        assert nid in node_ids

    # Reached + unreached partitions all function-kind nodes — no
    # function should be both reached and unreached.
    function_ids = {n.id for n in graph_blob.nodes if n.kind == "function"}
    reached = {s.node_id for f in flow_blob.flows for s in f.steps}
    assert (reached | set(flow_blob.unreached)) == function_ids
    assert reached.isdisjoint(flow_blob.unreached)


@pytest.mark.asyncio
async def test_file_set_hash_changes_when_workspace_file_changes(tmp_path: Path) -> None:
    """Sanity for the Phase 2 cache key — modifying a file changes its hash.

    This is the contract Phase 2 relies on for "skip relabel unless the
    flow's files changed." If this breaks silently, Phase 2's caching
    becomes either stale (no relabel) or pessimistic (always relabel).
    """
    ws = _setup_workspace(tmp_path)
    graph_blob = await run_pipeline(workspace=str(ws), commit_sha="abc1234")
    flow_blob_1 = derive_flow_blob(graph_blob, workspace_root=ws)

    if not flow_blob_1.flows:
        pytest.skip("fixture produced no flows; nothing to hash")

    flow = flow_blob_1.flows[0]
    # Modify the first file in the flow's file_set.
    target_path = ws / flow.file_set[0]
    original = target_path.read_text()
    target_path.write_text(original + "\n# modified\n")

    flow_blob_2 = derive_flow_blob(graph_blob, workspace_root=ws)
    flow_2 = next(f for f in flow_blob_2.flows if f.id == flow.id)
    assert flow_2.file_set_hash != flow.file_set_hash
