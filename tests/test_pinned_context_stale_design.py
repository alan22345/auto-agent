"""Stale design.md guard in pinned_context — task 29 incident (2026-05-27).

PR ergodic-ai/iot-apartment-simulator#53 was merged to main carrying
`.auto-agent/design.md` with `<!-- auto-agent: task_id=28 -->`. The next
task (29, "prettier apartment" rerun) cloned main, inherited the stale
file, and the architect's pinned context handed task-28's
counterfactual design to the architect as if it were the contract for
task 29. The architect dutifully re-submitted it.

Pin: when ``task_id`` is supplied, the pinned-context skips a
design.md whose header doesn't match. Fresh task ⇒ fresh design pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _write_design(workspace: Path, content: str) -> None:
    (workspace / ".auto-agent").mkdir(exist_ok=True)
    (workspace / ".auto-agent" / "design.md").write_text(content)


def test_pinned_context_skips_design_with_mismatched_task_header(tmp_path: Path):
    from agent.lifecycle.trio.pinned_context import build_pinned_block

    _write_design(
        tmp_path,
        "<!-- auto-agent: task_id=28 -->\n\n# Old design\n\nLeftover content.\n",
    )
    block = build_pinned_block(str(tmp_path), task_id=29)

    # The stale design.md must not appear in the pinned block.
    assert "Old design" not in block
    assert "Leftover content" not in block
    # And the design.md section header isn't emitted either — the file
    # is treated as if it didn't exist on disk.
    assert ".auto-agent/design.md" not in block


def test_pinned_context_includes_design_with_matching_task_header(tmp_path: Path):
    from agent.lifecycle.trio.pinned_context import build_pinned_block

    _write_design(
        tmp_path,
        "<!-- auto-agent: task_id=29 -->\n\n# Fresh design\n\nCorrect content.\n",
    )
    block = build_pinned_block(str(tmp_path), task_id=29)

    assert "Fresh design" in block
    assert "Correct content" in block
    assert ".auto-agent/design.md" in block


def test_pinned_context_accepts_design_without_header(tmp_path: Path):
    """Legacy artefacts predate the header convention — accept them."""
    from agent.lifecycle.trio.pinned_context import build_pinned_block

    _write_design(tmp_path, "# Legacy design\n\nNo header, written by an older auto-agent.\n")
    block = build_pinned_block(str(tmp_path), task_id=29)

    assert "Legacy design" in block


def test_pinned_context_no_task_id_keeps_legacy_behaviour(tmp_path: Path):
    """When the caller doesn't supply ``task_id`` (slice context, older
    callsites) we don't filter — pin whatever is on disk."""
    from agent.lifecycle.trio.pinned_context import build_pinned_block

    _write_design(tmp_path, "<!-- auto-agent: task_id=28 -->\n\n# Whatever\n")
    block = build_pinned_block(str(tmp_path))  # no task_id kwarg

    assert "Whatever" in block


def test_apply_pinned_artefacts_threads_task_id(tmp_path: Path):
    from agent.lifecycle.trio.pinned_context import (
        apply_pinned_artefacts_to_system_prompt,
    )

    _write_design(
        tmp_path,
        "<!-- auto-agent: task_id=28 -->\n\n# Counterfactual nonsense\n",
    )
    out = apply_pinned_artefacts_to_system_prompt(
        "Base prompt.",
        str(tmp_path),
        task_id=29,
    )

    # With the filter active the base prompt comes back untouched
    # (the stale design.md doesn't make it into the pinned block).
    assert out == "Base prompt."
