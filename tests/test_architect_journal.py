"""Architect externalized journal — ADR-015 §13.

Every architect decision (design submission, backlog submission, per-cycle
checkpoint) appends an entry to ``.auto-agent/architect_log.md`` and
writes a per-decision detail file to ``.auto-agent/decisions/<seq>.json``.

The sequence number is monotonically increasing across the parent task's
lifetime. Each entry captures: full decision payload + LLM rationale
(the prose accompanying the skill call) + timestamp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# next_decision_seq — monotonic counter scoped to a workspace.
# ---------------------------------------------------------------------------


def test_next_decision_seq_starts_at_one(tmp_path: Path) -> None:
    from agent.lifecycle.trio.journal import next_decision_seq

    assert next_decision_seq(str(tmp_path)) == 1


def test_next_decision_seq_is_monotonic(tmp_path: Path) -> None:
    """Each successive read returns the next unused seq number.

    The helper looks at existing files in ``.auto-agent/decisions/`` —
    seeding the directory with two prior records must yield ``3`` next.
    """

    from agent.lifecycle.trio.journal import next_decision_seq
    from agent.lifecycle.workspace_paths import AUTO_AGENT_DIR

    decisions = tmp_path / AUTO_AGENT_DIR / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "1.json").write_text("{}")
    (decisions / "2.json").write_text("{}")

    assert next_decision_seq(str(tmp_path)) == 3


def test_next_decision_seq_ignores_unrelated_files(tmp_path: Path) -> None:
    """Non-numeric files in ``decisions/`` are ignored."""

    from agent.lifecycle.trio.journal import next_decision_seq
    from agent.lifecycle.workspace_paths import AUTO_AGENT_DIR

    decisions = tmp_path / AUTO_AGENT_DIR / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "1.json").write_text("{}")
    (decisions / "README.md").write_text("notes")

    assert next_decision_seq(str(tmp_path)) == 2


# ---------------------------------------------------------------------------
# append_journal_entry — writes both files.
# ---------------------------------------------------------------------------


def test_append_journal_entry_writes_log_and_detail(tmp_path: Path) -> None:
    from agent.lifecycle.trio.journal import append_journal_entry
    from agent.lifecycle.workspace_paths import (
        ARCHITECT_LOG_PATH,
        decision_history_path,
    )

    decision = {
        "schema_version": "1",
        "action": "done",
        "payload": {},
    }
    rationale = "All backlog items shipped and the final review passed."

    seq = append_journal_entry(
        str(tmp_path),
        decision=decision,
        rationale=rationale,
    )

    assert seq == 1

    log_file = tmp_path / ARCHITECT_LOG_PATH
    assert log_file.is_file()
    log_text = log_file.read_text()
    # The log entry must reference the action and link to the detail file.
    assert "done" in log_text
    assert "decisions/1.json" in log_text
    # Rationale appears as a short prose blurb in the log too.
    assert "All backlog items shipped" in log_text

    detail_path = tmp_path / decision_history_path(1)
    assert detail_path.is_file()
    detail = json.loads(detail_path.read_text())
    assert detail["decision"] == decision
    assert detail["rationale"] == rationale
    assert "timestamp" in detail
    assert isinstance(detail["timestamp"], str)


def test_append_journal_entry_appends_to_existing_log(tmp_path: Path) -> None:
    from agent.lifecycle.trio.journal import append_journal_entry
    from agent.lifecycle.workspace_paths import ARCHITECT_LOG_PATH

    append_journal_entry(
        str(tmp_path),
        decision={"schema_version": "1", "action": "dispatch_new", "payload": {}},
        rationale="First decision",
    )
    seq2 = append_journal_entry(
        str(tmp_path),
        decision={"schema_version": "1", "action": "done", "payload": {}},
        rationale="Wrapping up",
    )

    assert seq2 == 2
    log_text = (tmp_path / ARCHITECT_LOG_PATH).read_text()
    assert "decisions/1.json" in log_text
    assert "decisions/2.json" in log_text
    # Old text is preserved — we APPEND, never rewrite.
    assert "First decision" in log_text
    assert "Wrapping up" in log_text


def test_append_journal_entry_creates_directories(tmp_path: Path) -> None:
    """``.auto-agent/`` and ``decisions/`` are created lazily."""

    from agent.lifecycle.trio.journal import append_journal_entry

    assert not (tmp_path / ".auto-agent").exists()
    append_journal_entry(
        str(tmp_path),
        decision={"schema_version": "1", "action": "done", "payload": {}},
        rationale="r",
    )
    assert (tmp_path / ".auto-agent" / "decisions").is_dir()
    assert (tmp_path / ".auto-agent" / "architect_log.md").is_file()


def test_append_journal_entry_records_action_header(tmp_path: Path) -> None:
    """The log entry's section header carries the decision action."""

    from agent.lifecycle.trio.journal import append_journal_entry
    from agent.lifecycle.workspace_paths import ARCHITECT_LOG_PATH

    append_journal_entry(
        str(tmp_path),
        decision={
            "schema_version": "1",
            "action": "spawn_sub_architects",
            "payload": {"slices": [{"name": "auth", "scope": "..."}]},
        },
        rationale="Task is huge — sub-architects it is.",
    )
    log_text = (tmp_path / ARCHITECT_LOG_PATH).read_text()
    assert "spawn_sub_architects" in log_text


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
