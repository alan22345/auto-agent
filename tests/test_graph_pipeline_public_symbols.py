"""``RepoGraphBlob.public_symbols`` is surfaced by the pipeline (ADR-016 Phase 6).

Phase 5 already computed the union of per-area public-symbol ids and used
it for boundary flagging — but kept the set internal to the pipeline. The
Phase 6 ``query_repo_graph.public_surface`` op needs to read it directly
from the stored blob, so the pipeline now plumbs the sorted list onto
the assembled :class:`shared.types.RepoGraphBlob`.

These tests pin the new shape without re-asserting Phase 5 flagging
behaviour (already covered by ``test_graph_pipeline_phase5.py``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline

_VIOLATIONS_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_violations_python"


def _setup(tmp_path: Path, fixture: Path) -> str:
    target = tmp_path / "workspace"
    shutil.copytree(fixture, target)
    return str(target)


@pytest.mark.asyncio
async def test_pipeline_plumbs_public_symbols_onto_blob(tmp_path: Path) -> None:
    ws = _setup(tmp_path, _VIOLATIONS_FIXTURE)
    blob = await run_pipeline(workspace=ws, commit_sha="abc")

    # public_symbols is the documented Phase 6 addition. The exact contents
    # vary with the fixture; we pin two invariants:
    #   1. The list is present and is a list of str.
    #   2. At least one well-known public symbol (PublicWidget) appears, and
    #      a well-known private symbol (_private_helper) does NOT.
    assert isinstance(blob.public_symbols, list)
    assert all(isinstance(sym, str) for sym in blob.public_symbols)
    assert any(s.endswith("::PublicWidget") for s in blob.public_symbols)
    assert all("_private_helper" not in s for s in blob.public_symbols)


@pytest.mark.asyncio
async def test_pipeline_public_symbols_is_sorted_and_deduplicated(
    tmp_path: Path,
) -> None:
    ws = _setup(tmp_path, _VIOLATIONS_FIXTURE)
    blob = await run_pipeline(workspace=ws, commit_sha="abc")

    # Sorted = deterministic across runs (matters because the JSONB blob
    # is persisted and we don't want spurious diffs).
    assert blob.public_symbols == sorted(blob.public_symbols)
    # No duplicates.
    assert len(blob.public_symbols) == len(set(blob.public_symbols))
