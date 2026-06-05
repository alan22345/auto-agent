"""Integration test: run_pipeline populates RepoGraphBlob.clones (ADR-016 Phase 11).

Fixture layout (tests/fixtures/graph_repo_clone_python/):

    dup_area/
        __init__.py
        alpha.py    # defines duplicated_processor — identical body to beta.py
        beta.py     # defines duplicated_processor — identical body to alpha.py

    other_area/
        __init__.py
        unique.py   # defines unique_helper — no duplication

Pipeline is run with default mode/min_tokens (mild, 50).
The two duplicated_processor functions each have >50 tokens so they MUST
produce a CloneGroup.  unique_helper must NOT appear in any clone group.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from shared.types import RepoGraphBlob

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_clone_python"


def _setup(tmp_path: Path) -> str:
    """Copy the clone fixture into tmp_path and return the workspace path."""
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


@pytest.mark.asyncio
async def test_pipeline_detects_duplicated_processor_clone(tmp_path: Path) -> None:
    """run_pipeline on the clone fixture produces a CloneGroup for the two
    duplicated_processor functions in alpha.py and beta.py."""
    ws = _setup(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="testsha_clone")

    assert isinstance(blob, RepoGraphBlob)
    assert isinstance(blob.clones, list), "blob.clones must be a list"

    # -----------------------------------------------------------------------
    # 1. There must be at least one clone group.
    # -----------------------------------------------------------------------
    assert len(blob.clones) >= 1, (
        f"Expected at least one clone group, got 0. "
        f"Nodes: {[n.id for n in blob.nodes if n.kind == 'function']}"
    )

    # -----------------------------------------------------------------------
    # 2. Find the group that contains both duplicated_processor functions.
    # -----------------------------------------------------------------------
    all_instance_ids = {inst.node_id for group in blob.clones for inst in group.instances}

    alpha_id = next(
        (
            n.id
            for n in blob.nodes
            if n.kind == "function" and "alpha" in n.file and "duplicated_processor" in n.id
        ),
        None,
    )
    beta_id = next(
        (
            n.id
            for n in blob.nodes
            if n.kind == "function" and "beta" in n.file and "duplicated_processor" in n.id
        ),
        None,
    )

    assert alpha_id is not None, (
        "Expected a function node for duplicated_processor in alpha.py; "
        f"got nodes: {[n.id for n in blob.nodes if n.kind == 'function']}"
    )
    assert beta_id is not None, (
        "Expected a function node for duplicated_processor in beta.py; "
        f"got nodes: {[n.id for n in blob.nodes if n.kind == 'function']}"
    )

    dup_group = next(
        (g for g in blob.clones if {inst.node_id for inst in g.instances} >= {alpha_id, beta_id}),
        None,
    )
    assert dup_group is not None, (
        f"Expected a clone group containing both {alpha_id!r} and {beta_id!r}. "
        f"Clone groups found: {[g.id for g in blob.clones]}"
    )

    # -----------------------------------------------------------------------
    # 3. The group must have both functions as instances.
    # -----------------------------------------------------------------------
    group_node_ids = {inst.node_id for inst in dup_group.instances}
    assert alpha_id in group_node_ids
    assert beta_id in group_node_ids

    # -----------------------------------------------------------------------
    # 4. unique_helper must NOT appear in any clone group.
    # -----------------------------------------------------------------------
    unique_id = next(
        (n.id for n in blob.nodes if n.kind == "function" and "unique_helper" in n.id),
        None,
    )
    if unique_id is not None:
        assert unique_id not in all_instance_ids, (
            f"unique_helper ({unique_id}) must not appear in any clone group"
        )

    # -----------------------------------------------------------------------
    # 5. Clone group has a stable, deterministic id.
    # -----------------------------------------------------------------------
    assert dup_group.id.startswith("clone:"), f"Unexpected id format: {dup_group.id!r}"
    assert dup_group.token_len >= 50, (
        f"Expected token_len >= 50 for the duplicated body, got {dup_group.token_len}"
    )
