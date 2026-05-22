"""Tests for the flow-level labeller (Phase 2).

Covers _load_file_slices first; per-flow LLM labelling and the
file-hash cache come in subsequent tasks.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from agent.graph_analyzer.flow_labeler import _load_file_slices
from shared.types import FlowStep, Node

if TYPE_CHECKING:
    from pathlib import Path


def _node(node_id: str, file: str, line_start: int, line_end: int) -> Node:
    return Node(
        id=node_id,
        kind="function",
        label=node_id.split("::")[-1],
        file=file,
        line_start=line_start,
        line_end=line_end,
        area="src",
    )


def test_load_slices_reads_lines_in_range(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "login.py").write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")

    nodes = {
        "api/login.py::login": _node("api/login.py::login", "api/login.py", 3, 7),
    }
    steps = [FlowStep(node_id="api/login.py::login", depth=0)]

    slices = _load_file_slices(tmp_path, steps, nodes)
    assert len(slices) == 1
    s = slices[0]
    assert s["file"] == "api/login.py"
    assert s["lines"] == [3, 7]
    # 5 lines (3,4,5,6,7) of content present.
    assert s["content"].count("\n") == 5
    assert "line 3" in s["content"]
    assert "line 7" in s["content"]
    assert "line 8" not in s["content"]


def test_load_slices_truncates_long_ranges(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "big.py").write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")

    nodes = {
        "api/big.py::huge": _node("api/big.py::huge", "api/big.py", 1, 100),
    }
    steps = [FlowStep(node_id="api/big.py::huge", depth=0)]

    slices = _load_file_slices(tmp_path, steps, nodes, max_lines_per_step=40)
    assert len(slices) == 1
    # Truncated to 40 lines.
    assert slices[0]["content"].count("\n") == 40
    assert slices[0]["lines"] == [1, 40]


def test_load_slices_skips_nodes_without_file_or_line_info(tmp_path: Path) -> None:
    # Node has no file / no line_start — skipped.
    nodes = {
        "unknown::x": Node(
            id="unknown::x", kind="function", label="x", area="src",
        ),
    }
    steps = [FlowStep(node_id="unknown::x", depth=0)]
    slices = _load_file_slices(tmp_path, steps, nodes)
    assert slices == []


def test_load_slices_skips_missing_files(tmp_path: Path) -> None:
    nodes = {
        "missing.py::x": _node("missing.py::x", "missing.py", 1, 5),
    }
    steps = [FlowStep(node_id="missing.py::x", depth=0)]
    slices = _load_file_slices(tmp_path, steps, nodes)
    assert slices == []


def test_load_slices_deduplicates_same_file_line_pair(tmp_path: Path) -> None:
    """If two steps point at the same (file, line_start, line_end), only
    one slice is returned — the LLM doesn't need it twice."""
    (tmp_path / "x.py").write_text("a\nb\nc\nd\ne\n")
    nodes = {
        "x.py::a": _node("x.py::a", "x.py", 1, 3),
        "x.py::b": _node("x.py::b", "x.py", 1, 3),  # same span as a
    }
    steps = [
        FlowStep(node_id="x.py::a", depth=0),
        FlowStep(node_id="x.py::b", depth=1),
    ]
    slices = _load_file_slices(tmp_path, steps, nodes)
    assert len(slices) == 1
