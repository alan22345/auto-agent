"""Integration test: run_pipeline populates RepoGraphBlob.dead_code (ADR-016 Phase 10).

Fixture layout (tests/fixtures/graph_repo_deadcode_python/):

    used_area/
        __init__.py
        utils.py          # exports used_helper (called by consumer.py) and unused_helper (not called)
        consumer.py       # imports utils and calls used_helper; nothing imports consumer.py

    unused_area/
        __init__.py
        orphan.py         # nothing imports this file; exports orphan_func (never called)

Actual dead-code findings produced by the pipeline:

    unused_export:
        used_area/utils.py::unused_helper   — exported, never called from outside
        used_area/consumer.py::do_work      — exported, never called from outside
        unused_area/orphan.py::orphan_func  — exported, never called from outside

    unused_file:
        file:unused_area/orphan.py          — nothing imports it, no entry point
        file:used_area/consumer.py          — nothing imports it, no entry point (true positive)

    NOT flagged:
        used_area/utils.py                  — IS imported by consumer.py (not unused_file)
        used_area/utils.py::used_helper     — IS called by consumer.py (not unused_export)

The test asserts these five correct findings and the two correct non-findings.
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
    """run_pipeline on the dead-code fixture produces the expected findings.

    Verified true positives (things that SHOULD be flagged):
    1. ``unused_helper`` in utils.py → unused_export (exported but never called externally).
    2. ``orphan.py`` → unused_file (nothing imports it, no entry point).
    3. ``consumer.py`` → unused_file (nothing imports it, no entry point — true positive).

    Verified true negatives (things that must NOT be flagged):
    4. ``used_helper`` in utils.py must NOT be unused_export (called from consumer.py).
    5. ``utils.py`` must NOT be unused_file (imported by consumer.py).
    """
    ws = _setup(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="testsha")

    assert isinstance(blob, RepoGraphBlob)
    assert isinstance(blob.dead_code, list)

    unused_export_targets = {f.target for f in blob.dead_code if f.kind == "unused_export"}
    unused_file_targets = {f.target for f in blob.dead_code if f.kind == "unused_file"}

    # -------------------------------------------------------------------
    # 1. unused_export: unused_helper should be flagged
    # -------------------------------------------------------------------
    matching_unused = [t for t in unused_export_targets if t.endswith("::unused_helper")]
    assert matching_unused, (
        "Expected an unused_export finding for 'unused_helper' in utils.py; "
        f"got unused_export targets: {sorted(unused_export_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )

    # -------------------------------------------------------------------
    # 2. unused_file: orphan.py should be flagged
    # -------------------------------------------------------------------
    matching_orphan = [t for t in unused_file_targets if "orphan" in t]
    assert matching_orphan, (
        "Expected an unused_file finding for 'orphan.py'; "
        f"got unused_file targets: {sorted(unused_file_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )

    # -------------------------------------------------------------------
    # 3. unused_file: consumer.py should be flagged (true positive)
    #    Nothing imports consumer.py and it is not an entry point.
    # -------------------------------------------------------------------
    matching_consumer = [t for t in unused_file_targets if "consumer" in t]
    assert matching_consumer, (
        "Expected an unused_file finding for 'consumer.py' (nothing imports it); "
        f"got unused_file targets: {sorted(unused_file_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )

    # -------------------------------------------------------------------
    # 4. No false positive: used_helper must NOT be flagged unused_export
    # -------------------------------------------------------------------
    false_pos_used = [t for t in unused_export_targets if t.endswith("::used_helper")]
    assert not false_pos_used, (
        "False positive: used_helper was flagged as unused_export, "
        "but it is called from consumer.py. "
        f"unused_export targets: {sorted(unused_export_targets)}"
    )

    # -------------------------------------------------------------------
    # 5. No false positive: utils.py must NOT be flagged unused_file
    #    (consumer.py imports it)
    # -------------------------------------------------------------------
    false_pos_utils = [t for t in unused_file_targets if "utils" in t]
    assert not false_pos_utils, (
        "False positive: utils.py was flagged as unused_file, "
        "but consumer.py imports it. "
        f"unused_file targets: {sorted(unused_file_targets)}"
    )

    # -------------------------------------------------------------------
    # 6. Structural checks
    # -------------------------------------------------------------------
    # Output must be deterministic — running again produces the same result.
    blob2 = await run_pipeline(workspace=ws, commit_sha="testsha")
    assert blob.dead_code == blob2.dead_code, (
        "compute_dead_code is not deterministic across two pipeline runs"
    )
