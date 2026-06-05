"""Integration test: run_pipeline populates RepoGraphBlob.dead_code (ADR-016 Phase 10).

Fixture layout (tests/fixtures/graph_repo_deadcode_python/):

    used_area/
        __init__.py
        utils.py          # exports used_helper (called externally) and unused_helper (not called)
        consumer.py       # imports utils and calls used_helper

    unused_area/
        __init__.py
        orphan.py         # nothing imports this file

The test asserts:
- blob.dead_code contains an ``unused_export`` finding for ``unused_helper`` in utils.py
- blob.dead_code contains an ``unused_file`` finding for ``unused_area/orphan.py``
- blob.dead_code does NOT contain an ``unused_export`` for ``used_helper``
- blob.dead_code does NOT contain an ``unused_file`` for ``used_area/consumer.py``
  (consumer.py is imported by no one, but we're checking unused_helper specifically;
  consumer.py itself may or may not be flagged depending on whether anything imports it)

Confirmed from pipeline.py _resolve_module_imports_to_files (~line 817-893):
After resolution, imports edges target ``file:<path>`` node ids — the ``n.id``
of nodes with kind="file", which is set to ``"file:" + n.file`` by the parser.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from shared.types import RepoGraphBlob

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_deadcode_python"


def _setup(tmp_path: Path) -> str:
    """Copy the dead-code fixture into tmp_path and return the workspace path."""
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


@pytest.mark.asyncio
async def test_pipeline_detects_unused_export_and_unused_file(tmp_path: Path) -> None:
    """run_pipeline on the dead-code fixture must:

    1. Flag ``unused_helper`` in utils.py as ``unused_export`` (it's exported
       but never called from outside its file).
    2. Flag ``orphan.py`` as ``unused_file`` (no import edge points to it).
    3. NOT flag ``used_helper`` as ``unused_export`` (it IS called from consumer.py).
    4. NOT flag ``consumer.py`` as ``unused_file`` via false positive — consumer.py
       imports utils.py (creating an import edge FROM consumer), but we verify
       utils.py is NOT flagged as unused_file since it IS imported.
    """
    ws = _setup(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="testsha")

    assert isinstance(blob, RepoGraphBlob)
    assert isinstance(blob.dead_code, list)

    # -------------------------------------------------------------------
    # 1. unused_export: unused_helper should be flagged
    # -------------------------------------------------------------------
    unused_export_targets = {f.target for f in blob.dead_code if f.kind == "unused_export"}
    # The node id form is "<rel_path>::<name>".  After pipeline runs from
    # workspace root the rel_path is relative to the workspace.
    matching_unused = [t for t in unused_export_targets if t.endswith("::unused_helper")]
    assert matching_unused, (
        "Expected an unused_export finding for 'unused_helper' in utils.py; "
        f"got unused_export targets: {sorted(unused_export_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )

    # -------------------------------------------------------------------
    # 2. unused_file: orphan.py should be flagged
    # -------------------------------------------------------------------
    unused_file_targets = {f.target for f in blob.dead_code if f.kind == "unused_file"}
    matching_orphan = [t for t in unused_file_targets if "orphan" in t]
    assert matching_orphan, (
        "Expected an unused_file finding for 'orphan.py'; "
        f"got unused_file targets: {sorted(unused_file_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )

    # -------------------------------------------------------------------
    # 3. No false positive: used_helper must NOT be flagged unused_export
    # -------------------------------------------------------------------
    false_pos_used = [t for t in unused_export_targets if t.endswith("::used_helper")]
    assert not false_pos_used, (
        "False positive: used_helper was flagged as unused_export, "
        "but it is called from consumer.py. "
        f"unused_export targets: {sorted(unused_export_targets)}"
    )

    # -------------------------------------------------------------------
    # 4. No false positive: utils.py must NOT be flagged unused_file
    #    (consumer.py imports it)
    # -------------------------------------------------------------------
    false_pos_utils = [t for t in unused_file_targets if "utils" in t]
    assert not false_pos_utils, (
        "False positive: utils.py was flagged as unused_file, "
        "but consumer.py imports it. "
        f"unused_file targets: {sorted(unused_file_targets)}"
    )

    # -------------------------------------------------------------------
    # 5. Structural checks
    # -------------------------------------------------------------------
    # Output must be deterministic — running again produces the same result.
    blob2 = await run_pipeline(workspace=ws, commit_sha="testsha")
    assert blob.dead_code == blob2.dead_code, (
        "compute_dead_code is not deterministic across two pipeline runs"
    )
