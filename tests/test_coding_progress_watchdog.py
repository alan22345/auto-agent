"""Progress-based coding liveness for the timeout watchdog.

Incident (task #327): the ``claude --print`` coder runs as one long subprocess
that sends no heartbeat, so the wall-clock watchdog killed a 60-min productive
run and restarted it in a loop. The fix judges a CODING task by whether its
workspace keeps changing — a progressing task is never timed out; only a
workspace static for 5 min is stalled.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import pytest

import run as run_module


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True)


def test_progress_stall_seconds_tracks_change(monkeypatch):
    monkeypatch.setattr(run_module, "_coding_progress", {})
    t0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)

    # First sighting → 0 (just started progressing)
    assert run_module._progress_stall_seconds(327, "sigA", t0) == 0.0
    # Same signature 4 min later → 240s stalled
    assert run_module._progress_stall_seconds(327, "sigA", t0 + timedelta(minutes=4)) == 240.0
    # Signature changes → progress, resets to 0
    assert run_module._progress_stall_seconds(327, "sigB", t0 + timedelta(minutes=5)) == 0.0
    # Static again from the new baseline
    assert run_module._progress_stall_seconds(327, "sigB", t0 + timedelta(minutes=11)) == 360.0


@pytest.mark.asyncio
async def test_workspace_signature_changes_on_edit(tmp_path):
    # Non-git dir → None (caller falls back to heartbeat path)
    assert await run_module._workspace_signature(str(tmp_path)) is None

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    sig1 = await run_module._workspace_signature(str(tmp_path))
    assert sig1 is not None

    # An in-flight working-tree edit must change the signature (progress)
    (tmp_path / "b.txt").write_text("two")
    sig2 = await run_module._workspace_signature(str(tmp_path))
    assert sig2 != sig1

    # A new commit must also change it
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "more")
    sig3 = await run_module._workspace_signature(str(tmp_path))
    assert sig3 != sig2
