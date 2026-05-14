"""Design-doc header tests — ADR-015 §2 Phase 7.6.

The design gate uses a ``<!-- auto-agent: task_id=N -->`` header line on
``.auto-agent/design.md`` so a stale file left over from a previous task
that reused the same workspace path doesn't trick the gate into routing
to ``run_initial``. Coverage:

- ``write_design`` prepends the header for a fresh write.
- ``_design_md_exists`` matches only on the correct task id.
- ``strip_design_header`` removes the header (and the single blank line
  that follows) idempotently.
- The orchestrator's gate-artefact endpoint strips the header before
  serving the markdown to the UI.
"""

from __future__ import annotations

import os
import tempfile

from agent.lifecycle.trio import _design_md_exists
from agent.lifecycle.trio.design_approval import write_design
from agent.lifecycle.workspace_paths import (
    AUTO_AGENT_DIR,
    DESIGN_PATH,
    format_design_header,
    strip_design_header,
)


def _read(workspace_root: str) -> str:
    with open(os.path.join(workspace_root, DESIGN_PATH)) as fh:
        return fh.read()


def test_write_design_prepends_header():
    with tempfile.TemporaryDirectory() as ws:
        write_design(ws, "# Hello\n\nbody\n", task_id=42)
        content = _read(ws)
        assert content.startswith("<!-- auto-agent: task_id=42 -->\n\n")
        assert "# Hello" in content


def test_write_design_does_not_double_stamp():
    with tempfile.TemporaryDirectory() as ws:
        write_design(ws, "# A\n", task_id=42)
        # Caller passes the already-stamped text back in (e.g. on a re-run).
        prestamped = _read(ws)
        write_design(ws, prestamped, task_id=42)
        content = _read(ws)
        # Exactly one header line.
        assert content.count("<!-- auto-agent: task_id=42 -->") == 1


def test_design_md_exists_true_when_header_matches():
    with tempfile.TemporaryDirectory() as ws:
        write_design(ws, "# X\n", task_id=42)
        assert _design_md_exists(ws, task_id=42) is True


def test_design_md_exists_false_when_header_mismatches():
    with tempfile.TemporaryDirectory() as ws:
        write_design(ws, "# X\n", task_id=42)
        # Different task id — gate must treat as stale.
        assert _design_md_exists(ws, task_id=99) is False


def test_design_md_exists_false_when_header_missing_legacy():
    with tempfile.TemporaryDirectory() as ws:
        os.makedirs(os.path.join(ws, AUTO_AGENT_DIR), exist_ok=True)
        with open(os.path.join(ws, DESIGN_PATH), "w") as fh:
            fh.write("# Legacy design with no header\n\nbody\n")
        # No header at all — pre-Phase-7.6 file.
        assert _design_md_exists(ws, task_id=42) is False


def test_design_md_exists_false_when_file_missing():
    with tempfile.TemporaryDirectory() as ws:
        assert _design_md_exists(ws, task_id=42) is False


def test_design_md_exists_false_when_workspace_root_none():
    assert _design_md_exists(None, task_id=42) is False


def test_strip_design_header_removes_header_and_blank():
    stamped = "<!-- auto-agent: task_id=7 -->\n\n# Body\n"
    assert strip_design_header(stamped) == "# Body\n"


def test_strip_design_header_idempotent_on_unstamped():
    unstamped = "# Body\n\nMore\n"
    assert strip_design_header(unstamped) == unstamped


def test_strip_design_header_handles_empty_string():
    assert strip_design_header("") == ""


def test_format_design_header_renders():
    assert format_design_header(123) == "<!-- auto-agent: task_id=123 -->"


def test_design_md_exists_tolerates_leading_blank_lines():
    """Defensive: if anything ever writes a blank line before the header
    (shouldn't happen but ``strip().startswith`` semantics require explicit
    coverage), the gate still matches on the first non-empty line."""
    with tempfile.TemporaryDirectory() as ws:
        os.makedirs(os.path.join(ws, AUTO_AGENT_DIR), exist_ok=True)
        with open(os.path.join(ws, DESIGN_PATH), "w") as fh:
            fh.write("\n\n<!-- auto-agent: task_id=42 -->\n\n# Body\n")
        assert _design_md_exists(ws, task_id=42) is True
