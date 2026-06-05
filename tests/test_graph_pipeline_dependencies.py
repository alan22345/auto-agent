"""Integration test: run_pipeline populates blob.dead_code with dependency findings
(ADR-016 Phase 10 §4b).

Fixture layout (tests/fixtures/graph_repo_deps_python/):

    pyproject.toml
        [project].dependencies = [used_pkg, unused_pkg]

    used_area/
        __init__.py
        main.py     # imports used_pkg, undeclared_pkg, os, first_party_area

    first_party_area/
        __init__.py
        helper.py

Expected dead_code dependency findings produced by the pipeline:

    unused_dependency:
        unused_pkg   — declared in pyproject.toml but never imported

    undeclared_dependency:
        undeclared_pkg  — imported in main.py but not declared in pyproject.toml

NOT flagged:
    used_pkg        — declared AND imported
    os              — Python stdlib
    first_party_area — first-party (file node exists in the graph)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from shared.types import RepoGraphBlob

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_deps_python"


def _setup(tmp_path: Path) -> str:
    """Copy the deps fixture into tmp_path and return the workspace path."""
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


@pytest.mark.asyncio
async def test_pipeline_detects_dependency_findings(tmp_path: Path) -> None:
    """run_pipeline on the deps fixture produces correct dependency findings."""
    ws = _setup(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="testsha-deps")

    assert isinstance(blob, RepoGraphBlob)
    assert isinstance(blob.dead_code, list)

    unused_targets = {f.target for f in blob.dead_code if f.kind == "unused_dependency"}
    undeclared_targets = {f.target for f in blob.dead_code if f.kind == "undeclared_dependency"}

    # -------------------------------------------------------------------
    # 1. unused_dependency: unused_pkg should be flagged
    # -------------------------------------------------------------------
    assert "unused-pkg" in unused_targets or "unused_pkg" in unused_targets, (
        "Expected unused_dependency for 'unused_pkg'; "
        f"got unused_dependency targets: {sorted(unused_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )

    # -------------------------------------------------------------------
    # 2. undeclared_dependency: undeclared_pkg should be flagged
    # -------------------------------------------------------------------
    assert "undeclared_pkg" in undeclared_targets, (
        "Expected undeclared_dependency for 'undeclared_pkg'; "
        f"got undeclared_dependency targets: {sorted(undeclared_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )

    # -------------------------------------------------------------------
    # 3. No false positive: used_pkg must NOT be unused_dependency
    # -------------------------------------------------------------------
    assert "used-pkg" not in unused_targets and "used_pkg" not in unused_targets, (
        "False positive: used_pkg declared AND imported, must not be unused_dependency. "
        f"unused_dependency targets: {sorted(unused_targets)}"
    )

    # -------------------------------------------------------------------
    # 4. No false positive: os (stdlib) must NOT be undeclared
    # -------------------------------------------------------------------
    assert "os" not in undeclared_targets, (
        "False positive: os is stdlib, must not be undeclared_dependency. "
        f"undeclared_dependency targets: {sorted(undeclared_targets)}"
    )

    # -------------------------------------------------------------------
    # 5. No false positive: first_party_area must NOT be undeclared
    # -------------------------------------------------------------------
    assert "first_party_area" not in undeclared_targets, (
        "False positive: first_party_area is first-party, must not be undeclared_dependency. "
        f"undeclared_dependency targets: {sorted(undeclared_targets)}"
    )

    # -------------------------------------------------------------------
    # 6. Determinism
    # -------------------------------------------------------------------
    blob2 = await run_pipeline(workspace=ws, commit_sha="testsha-deps")
    dep_findings_1 = [
        (f.kind, f.target)
        for f in blob.dead_code
        if f.kind in {"unused_dependency", "undeclared_dependency"}
    ]
    dep_findings_2 = [
        (f.kind, f.target)
        for f in blob2.dead_code
        if f.kind in {"unused_dependency", "undeclared_dependency"}
    ]
    assert dep_findings_1 == dep_findings_2, (
        "Dependency dead_code findings must be deterministic across two pipeline runs"
    )
