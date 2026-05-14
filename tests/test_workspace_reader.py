"""Spec for ``agent.lifecycle.workspace_reader`` — ADR-015 §12.

The orchestrator reads skill-emitted files from the workspace after
``agent.run`` returns. This module owns the read primitives. Three
functions:

- :func:`read_gate_file` — returns parsed dict (``.json``), markdown text
  (``.md``), or ``None`` when the file is missing. Validates
  ``schema_version`` when present in JSON.
- :func:`gate_file_exists` — boolean existence check.
- :func:`expect_gate_file` — like ``read_gate_file`` but raises
  :class:`MissingGateFileError` when missing. Used by the retry-then-
  escalate orchestrator path (later phases).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent.lifecycle.workspace_reader import (
    MissingGateFileError,
    expect_gate_file,
    gate_file_exists,
    read_gate_file,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# read_gate_file
# ---------------------------------------------------------------------------


def test_read_gate_file_missing_returns_none(tmp_path: Path) -> None:
    assert read_gate_file(str(tmp_path), ".auto-agent/grill.json") is None


def test_read_gate_file_json_parses_dict(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent" / "grill.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"schema_version": "1", "summary": "ok"}))

    out = read_gate_file(str(tmp_path), ".auto-agent/grill.json")
    assert out == {"schema_version": "1", "summary": "ok"}


def test_read_gate_file_json_without_schema_version_returns_dict(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent" / "plan_approval.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"verdict": "approved", "comments": ""}))

    out = read_gate_file(str(tmp_path), ".auto-agent/plan_approval.json")
    assert out == {"verdict": "approved", "comments": ""}


def test_read_gate_file_json_schema_mismatch_raises(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent" / "grill.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"schema_version": "2", "summary": "ok"}))

    with pytest.raises(ValueError):
        read_gate_file(str(tmp_path), ".auto-agent/grill.json", schema_version="1")


def test_read_gate_file_json_explicit_schema_match(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent" / "grill.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"schema_version": "1", "summary": "ok"}))

    out = read_gate_file(str(tmp_path), ".auto-agent/grill.json", schema_version="1")
    assert out == {"schema_version": "1", "summary": "ok"}


def test_read_gate_file_markdown_returns_string(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent" / "plan.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Plan\n\nDo the thing.")

    out = read_gate_file(str(tmp_path), ".auto-agent/plan.md")
    assert out == "# Plan\n\nDo the thing."


def test_read_gate_file_invalid_json_raises(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent" / "decision.json"
    target.parent.mkdir(parents=True)
    target.write_text("not valid json{{{")

    with pytest.raises(ValueError):
        read_gate_file(str(tmp_path), ".auto-agent/decision.json")


# ---------------------------------------------------------------------------
# gate_file_exists
# ---------------------------------------------------------------------------


def test_gate_file_exists_false_when_missing(tmp_path: Path) -> None:
    assert gate_file_exists(str(tmp_path), ".auto-agent/grill.json") is False


def test_gate_file_exists_true_when_present(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent" / "grill.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}")

    assert gate_file_exists(str(tmp_path), ".auto-agent/grill.json") is True


# ---------------------------------------------------------------------------
# expect_gate_file
# ---------------------------------------------------------------------------


def test_expect_gate_file_raises_on_missing(tmp_path: Path) -> None:
    with pytest.raises(MissingGateFileError):
        expect_gate_file(str(tmp_path), ".auto-agent/grill.json")


def test_expect_gate_file_returns_value_when_present(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent" / "design.md"
    target.parent.mkdir(parents=True)
    target.write_text("# design")

    out = expect_gate_file(str(tmp_path), ".auto-agent/design.md")
    assert out == "# design"


def test_expect_gate_file_passes_through_schema_check(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent" / "grill.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"schema_version": "9"}))

    with pytest.raises(ValueError):
        expect_gate_file(str(tmp_path), ".auto-agent/grill.json", schema_version="1")
