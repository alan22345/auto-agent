"""Integration test: run_pipeline populates blob.health and blob.file_health (ADR-016 §6 Phase 13).

Uses the dead-code fixture (tests/fixtures/graph_repo_deadcode_python) which has:
  - unused_area/orphan.py: flagged as unused_file + unused_export (dead file)
  - used_area/utils.py: has both a used and an unused export (partially dead)
  - used_area/consumer.py: flagged as unused_file

Assertions:
  1. blob.health is not None after run_pipeline on the dead-code fixture.
  2. blob.file_health is non-empty.
  3. The file with an unused_file finding has a lower MI than utils.py (which is imported,
     thus NOT flagged as unused_file — only one unused_export lowers it modestly).
  4. health is populated even on a NON-git workspace (fixture is not a git repo),
     proving health does not depend on churn/git.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from shared.types import RepoGraphBlob

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_deadcode_python"


def _setup(tmp_path: Path) -> str:
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


@pytest.mark.asyncio
async def test_health_populated_on_dead_code_fixture(tmp_path: Path) -> None:
    """run_pipeline on the dead-code fixture (non-git) populates health fields."""
    ws = _setup(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="testsha")

    assert isinstance(blob, RepoGraphBlob)

    # 1. blob.health must not be None.
    assert blob.health is not None, "blob.health should be populated after run_pipeline"

    # 2. blob.file_health must be non-empty.
    assert len(blob.file_health) > 0, "blob.file_health should contain at least one entry"

    # 3. Files flagged as unused_file should have lower MI than utils.py.
    # The dead-code fixture marks orphan.py and consumer.py as unused_file.
    # utils.py is imported by consumer.py, so it is NOT an unused_file,
    # but it does have one unused_export (unused_helper) which slightly lowers its MI.
    # Find entries whose file paths contain "orphan.py" or "consumer.py"
    dead_file_entries = [
        fh for fh in blob.file_health if "orphan.py" in fh.file or ("consumer.py" in fh.file)
    ]
    utils_entries = [fh for fh in blob.file_health if fh.file.endswith("utils.py")]

    assert dead_file_entries, (
        "Expected file_health entries for files flagged as unused_file (orphan.py or consumer.py)"
    )
    assert utils_entries, "Expected a file_health entry for utils.py"

    utils_mi = utils_entries[0].maintainability_index
    for dead_fh in dead_file_entries:
        assert dead_fh.maintainability_index < utils_mi, (
            f"{dead_fh.file} (MI={dead_fh.maintainability_index:.2f}) should be lower than "
            f"utils.py (MI={utils_mi:.2f}) because it is flagged as unused_file"
        )

    # 4. Confirm this is a non-git workspace by checking hotspots is empty
    # (hotspots require git history; fixtures are plain directories).
    assert blob.hotspots == [], (
        "Fixture is not a git repo so hotspots should be empty — "
        "confirming health works without git"
    )

    # 5. Health score should be a reasonable float in [0, 100].
    assert 0.0 <= blob.health.score <= 100.0
