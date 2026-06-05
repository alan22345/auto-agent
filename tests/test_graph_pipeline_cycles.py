"""Integration test: run_pipeline populates RepoGraphBlob.cycles (ADR-016 Phase 9).

Sets up a tiny fixture workspace containing two Python modules that mutually import
each other (cycle_area/alpha.py <-> cycle_area/beta.py), runs the full pipeline,
and asserts that the resulting RepoGraphBlob.cycles is non-empty and contains
the expected member modules.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from shared.types import RepoGraphBlob

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_cycle_python"


def _setup(tmp_path: Path) -> str:
    """Copy the cycle fixture into tmp_path and return the workspace path."""
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


@pytest.mark.asyncio
async def test_pipeline_detects_import_cycle(tmp_path: Path) -> None:
    """run_pipeline on a workspace with alpha <-> beta import cycle must
    produce a RepoGraphBlob with at least one cycle whose members include
    the two mutually-importing modules."""
    ws = _setup(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="testsha")

    assert isinstance(blob, RepoGraphBlob)
    assert blob.cycles, (
        "Expected at least one import cycle detected from alpha <-> beta mutual imports; "
        f"got zero. Edges: {[e for e in blob.edges if e.kind == 'imports']}"
    )

    # Find the cycle that contains both alpha and beta module ids.
    # After pipeline resolution, module ids may be file: ids — check both
    # module: and file: forms.
    def _matches_cycle(cycle_members: list[str]) -> bool:
        joined = " ".join(cycle_members)
        return ("alpha" in joined) and ("beta" in joined)

    matching = [c for c in blob.cycles if _matches_cycle(c.members)]
    assert matching, (
        "Expected a cycle whose members include both 'alpha' and 'beta'; "
        f"got cycles: {[(c.id, c.members) for c in blob.cycles]}"
    )

    cycle = matching[0]
    assert cycle.kind == "import"
    assert len(cycle.members) >= 2
    assert len(cycle.closing_edges) >= 1
    # id must be stable (deterministic)
    assert cycle.id.startswith("cycle:")
