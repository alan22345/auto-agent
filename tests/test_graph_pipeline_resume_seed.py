"""Regression: run_pipeline must preserve initial_blob nodes/edges.

Background — 2026-05-20 incident. After 594/607 files were processed and
checkpointed (3012 nodes / 6056 edges in ``r.graph_json``), the user
clicked Refresh to resume. The skip-gate correctly skipped all 594 files
and the pipeline processed only the 13 remaining files. But run_pipeline
returned a ``RepoGraphBlob`` built from ``all_nodes``/``all_edges``,
which were initialized empty and only collected newly-processed files'
output. ``graph_refresh.run_refresh`` then did
``r.graph_json = json.loads(blob.model_dump_json())`` and overwrote the
6056 edges with the ~0 edges from the 13-file blob.

These tests guard against that overwrite by exercising run_pipeline's
contract directly: when resume state is supplied, the returned blob
must include the inherited work.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_python"


def _setup_workspace(tmp_path: Path) -> str:
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


def _inherited_node_dict(node_id: str, file: str = "agent_area/legacy.py") -> dict:
    return {
        "id": node_id,
        "kind": "function",
        "label": node_id.split("::")[-1],
        "file": file,
        "line_start": 1,
        "line_end": 5,
        "area": "agent_area",
        "parent": None,
        "decorators": [],
    }


def _inherited_edge_dict(source: str, target: str, file: str = "agent_area/legacy.py") -> dict:
    return {
        "source": source,
        "target": target,
        "kind": "calls",
        "evidence": {"file": file, "line": 2, "snippet": "<inherited>"},
        "source_kind": "ast",
        "boundary_violation": False,
        "violation_reason": None,
    }


async def _noop_checkpoint(blob_dict, processed_files, failed_sites) -> None:
    return None


@pytest.mark.asyncio
async def test_resume_preserves_inherited_nodes_and_edges(tmp_path: Path) -> None:
    """The returned blob includes nodes/edges from initial_blob even when
    no new files are processed (every file in initial_processed_files)."""
    workspace = _setup_workspace(tmp_path)

    # List every source file in the fixture so the skip-gate skips them
    # all and we exercise the pure-inheritance path.
    import os
    processed_files: dict[str, dict] = {}
    for root, _dirs, files in os.walk(workspace):
        for f in files:
            if f.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
                rel = os.path.relpath(os.path.join(root, f), workspace).replace(os.sep, "/")
                processed_files[rel] = {
                    "sites_attempted": 0,
                    "sites_succeeded": 0,
                    "edges_added": 0,
                    "processed_at": "2026-05-19T16:10:01Z",
                }

    inherited_nodes = [
        _inherited_node_dict("legacy::alpha"),
        _inherited_node_dict("legacy::beta"),
    ]
    inherited_edges = [
        _inherited_edge_dict("legacy::alpha", "legacy::beta"),
    ]
    initial_blob = {
        "commit_sha": "deadbeef",
        "areas": [],
        "nodes": inherited_nodes,
        "edges": inherited_edges,
        "public_symbols": ["legacy::alpha"],
    }

    blob = await run_pipeline(
        workspace=workspace,
        commit_sha="deadbeef",
        on_file_checkpoint=_noop_checkpoint,
        initial_processed_files=processed_files,
        initial_failed_sites=[],
        initial_blob=initial_blob,
    )

    returned_node_ids = {n.id for n in blob.nodes}
    assert "legacy::alpha" in returned_node_ids, (
        "Inherited node `legacy::alpha` was dropped from the returned blob — "
        "this is the bug that overwrote 3012 nodes with 19 in prod."
    )
    assert "legacy::beta" in returned_node_ids
    returned_edge_keys = {(e.source, e.target) for e in blob.edges}
    assert ("legacy::alpha", "legacy::beta") in returned_edge_keys, (
        "Inherited edge was dropped — same bug that overwrote 6056 edges with 0."
    )
    assert "legacy::alpha" in blob.public_symbols


@pytest.mark.asyncio
async def test_resume_combines_inherited_and_new_nodes(tmp_path: Path) -> None:
    """When initial_blob is supplied and new files exist, the returned blob
    contains both inherited and freshly-parsed nodes."""
    workspace = _setup_workspace(tmp_path)

    inherited_nodes = [_inherited_node_dict("legacy::alpha")]
    inherited_edges = [_inherited_edge_dict("legacy::alpha", "legacy::alpha")]
    initial_blob = {
        "commit_sha": "deadbeef",
        "areas": [],
        "nodes": inherited_nodes,
        "edges": inherited_edges,
        "public_symbols": [],
    }

    blob = await run_pipeline(
        workspace=workspace,
        commit_sha="deadbeef",
        on_file_checkpoint=_noop_checkpoint,
        initial_processed_files={},  # nothing skipped — process everything fresh
        initial_failed_sites=[],
        initial_blob=initial_blob,
    )

    returned_node_ids = {n.id for n in blob.nodes}
    assert "legacy::alpha" in returned_node_ids, "Inherited node missing"
    # And the freshly-parsed fixture nodes are still there too.
    assert any(n.file and n.file.endswith(".py") for n in blob.nodes), (
        "Newly-parsed nodes missing from the returned blob"
    )


@pytest.mark.asyncio
async def test_no_resume_state_behaves_like_before(tmp_path: Path) -> None:
    """When no resume state is supplied (no on_file_checkpoint), the seed
    path is skipped and the pipeline behaves exactly as in a non-resume
    full run. Guards against accidental regressions in the seed branch."""
    workspace = _setup_workspace(tmp_path)
    blob = await run_pipeline(workspace=workspace, commit_sha="deadbeef")
    assert blob.commit_sha == "deadbeef"
    assert len(blob.nodes) > 0
