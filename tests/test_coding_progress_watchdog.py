"""Progress-based coding liveness for the timeout watchdog.

Incident (task #327): the ``claude --print`` coder runs as one long subprocess
that sends no heartbeat, so the wall-clock watchdog killed a 60-min productive
run and restarted it in a loop. The fix judges a CODING task by whether its
workspace keeps changing — a progressing task is never timed out; only a
workspace static for 5 min is stalled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import run as run_module


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
    # Missing dir → None (caller falls back to heartbeat path)
    assert await run_module._workspace_signature(str(tmp_path / "nope")) is None

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("one")
    sig1 = await run_module._workspace_signature(str(ws))
    assert sig1 is not None

    # A new file changes the count → signature changes (progress)
    (ws / "b.txt").write_text("two")
    sig2 = await run_module._workspace_signature(str(ws))
    assert sig2 != sig1

    # The .git dir is ignored — committing internals is not "progress"
    (ws / ".git").mkdir()
    (ws / ".git" / "objects").write_text("x")
    sig3 = await run_module._workspace_signature(str(ws))
    assert sig3 == sig2
