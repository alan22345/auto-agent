"""Unit tests for agent.po_graph_findings.summarize_graph_findings.

Core contract tested here:
  - A complex function in a HOTSPOT file appears in the output.
  - A complex function in a NON-HOTSPOT file does NOT appear (churn gate).
  - Clone groups with 3+ instances where any instance is in a hotspot → appears.
  - Clone groups entirely in non-hotspot files → omitted.
  - 2-instance tiny clones (token_len < 80) → omitted.
  - Import cycles always appear.
  - Dead code always appears.
  - Empty blob → returns ''.
  - Output is deterministic (two calls equal).
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.po_graph_findings import summarize_graph_findings
from shared.types import (
    CloneGroup,
    CloneInstance,
    DeadCodeFinding,
    DependencyCycle,
    EdgeEvidence,
    FileHealth,
    Hotspot,
    Node,
    RepoGraphBlob,
    RepoHealth,
)


def _minimal_blob(**kwargs) -> RepoGraphBlob:
    """Construct a RepoGraphBlob with required fields set to safe defaults."""
    defaults = dict(
        commit_sha="abc123",
        generated_at=datetime(2024, 1, 1, tzinfo=UTC),
        analyser_version="test",
        areas=[],
        nodes=[],
        edges=[],
    )
    defaults.update(kwargs)
    return RepoGraphBlob(**defaults)


def _hotspot(file: str, score: float = 60.0) -> Hotspot:
    return Hotspot(
        file=file,
        churn=5.0,
        complexity_density=0.5,
        score=score,
        trend="stable",
    )


def _func_node(
    node_id: str,
    file: str,
    *,
    cyclomatic: int | None = None,
    cognitive: int | None = None,
    line_start: int = 1,
    line_end: int = 50,
) -> Node:
    return Node(
        id=node_id,
        kind="function",
        label=node_id,
        file=file,
        line_start=line_start,
        line_end=line_end,
        area="agent",
        cyclomatic=cyclomatic,
        cognitive=cognitive,
    )


# ---------------------------------------------------------------------------
# Empty blob
# ---------------------------------------------------------------------------


def test_empty_blob_returns_empty_string():
    blob = _minimal_blob()
    assert summarize_graph_findings(blob) == ""


# ---------------------------------------------------------------------------
# Churn gate — complex functions
# ---------------------------------------------------------------------------


def test_complex_function_in_hotspot_file_appears():
    """A cyclomatic-30 function in a hotspot file MUST appear in the output."""
    blob = _minimal_blob(
        hotspots=[_hotspot("agent/foo.py", score=60)],
        nodes=[
            _func_node("agent/foo.py::my_func", "agent/foo.py", cyclomatic=30),
        ],
    )
    result = summarize_graph_findings(blob)
    assert "agent/foo.py::my_func" in result
    assert "cyclomatic=30" in result


def test_complex_function_in_non_hotspot_file_omitted():
    """A cyclomatic-30 function in a NON-hotspot file must NOT appear (churn gate)."""
    blob = _minimal_blob(
        hotspots=[_hotspot("agent/foo.py", score=60)],
        nodes=[
            # This function is complex but lives in a frozen (non-hotspot) file
            _func_node("agent/frozen.py::heavy_func", "agent/frozen.py", cyclomatic=30),
        ],
    )
    result = summarize_graph_findings(blob)
    # frozen.py has no hotspot, so heavy_func must be omitted
    assert "heavy_func" not in result
    assert "agent/frozen.py::heavy_func" not in result


def test_churn_gate_is_enforced_explicitly():
    """Hotspot file → complex function appears; non-hotspot file → omitted.

    This is the core value of the churn gate.  Both functions have identical
    complexity; only their file membership determines inclusion.
    """
    blob = _minimal_blob(
        hotspots=[_hotspot("hot/file.py", score=70)],
        nodes=[
            _func_node("hot/file.py::func_in_hot", "hot/file.py", cyclomatic=25),
            _func_node("cold/file.py::func_in_cold", "cold/file.py", cyclomatic=25),
        ],
    )
    result = summarize_graph_findings(blob)
    assert "func_in_hot" in result, "function in hotspot file must appear"
    assert "func_in_cold" not in result, "function in cold (non-hotspot) file must be OMITTED"


def test_cognitive_threshold_triggers_churn_gate():
    """Cognitive >= 15 in hotspot file also triggers inclusion."""
    blob = _minimal_blob(
        hotspots=[_hotspot("agent/bar.py", score=55)],
        nodes=[
            _func_node("agent/bar.py::complex_cog", "agent/bar.py", cognitive=20),
        ],
    )
    result = summarize_graph_findings(blob)
    assert "complex_cog" in result
    assert "cognitive=20" in result


def test_below_threshold_function_in_hotspot_omitted():
    """A cyclomatic-5 function in a hotspot file is NOT complex enough to surface."""
    blob = _minimal_blob(
        hotspots=[_hotspot("agent/foo.py", score=60)],
        nodes=[
            _func_node("agent/foo.py::simple_func", "agent/foo.py", cyclomatic=5),
        ],
    )
    result = summarize_graph_findings(blob)
    assert "simple_func" not in result


# ---------------------------------------------------------------------------
# Hotspot score threshold
# ---------------------------------------------------------------------------


def test_hotspot_below_min_score_does_not_include_its_functions():
    """A hotspot with score < hotspot_min_score does not count as a hotspot."""
    blob = _minimal_blob(
        hotspots=[_hotspot("agent/low.py", score=30)],  # below default 50
        nodes=[
            _func_node("agent/low.py::complex_func", "agent/low.py", cyclomatic=25),
        ],
    )
    result = summarize_graph_findings(blob, hotspot_min_score=50.0)
    assert "complex_func" not in result


# ---------------------------------------------------------------------------
# Clone groups — churn gate + significance gate
# ---------------------------------------------------------------------------


def _clone_group(
    group_id: str,
    token_len: int,
    instance_files: list[str],
) -> CloneGroup:
    instances = [
        CloneInstance(node_id=f"n{i}", file=f, line_start=1, line_end=10)
        for i, f in enumerate(instance_files)
    ]
    return CloneGroup(id=group_id, token_len=token_len, mode="strict", instances=instances)


def test_clone_group_with_3_instances_and_hotspot_file_appears():
    """A clone group with 3 instances where one is in a hotspot → appears."""
    blob = _minimal_blob(
        hotspots=[_hotspot("hot/a.py", score=60)],
        clones=[
            _clone_group(
                "clone-1", token_len=50, instance_files=["hot/a.py", "cold/b.py", "cold/c.py"]
            ),
        ],
    )
    result = summarize_graph_findings(blob)
    assert "clone-1" in result


def test_clone_group_entirely_in_non_hotspot_files_omitted():
    """A clone group with no hotspot instances → omitted (churn gate)."""
    blob = _minimal_blob(
        hotspots=[_hotspot("hot/a.py", score=60)],
        clones=[
            _clone_group(
                "cold-clone", token_len=100, instance_files=["cold/x.py", "cold/y.py", "cold/z.py"]
            ),
        ],
    )
    result = summarize_graph_findings(blob)
    assert "cold-clone" not in result


def test_tiny_clone_two_instances_omitted():
    """A 2-instance clone with token_len < 80 is too small to surface."""
    blob = _minimal_blob(
        hotspots=[_hotspot("hot/a.py", score=60)],
        clones=[
            _clone_group("tiny-clone", token_len=40, instance_files=["hot/a.py", "cold/b.py"]),
        ],
    )
    result = summarize_graph_findings(blob)
    assert "tiny-clone" not in result


def test_large_token_clone_two_instances_appears_if_hotspot():
    """A 2-instance clone with token_len >= 80 is significant enough to surface."""
    blob = _minimal_blob(
        hotspots=[_hotspot("hot/a.py", score=60)],
        clones=[
            _clone_group("big-clone", token_len=120, instance_files=["hot/a.py", "cold/b.py"]),
        ],
    )
    result = summarize_graph_findings(blob)
    assert "big-clone" in result


# ---------------------------------------------------------------------------
# Cycles — always reported
# ---------------------------------------------------------------------------


def test_import_cycle_always_appears():
    """Import cycles appear regardless of hotspot status."""
    cycle = DependencyCycle(
        id="cyc-1",
        kind="import",
        members=["module:agent.a", "module:agent.b"],
        closing_edges=[EdgeEvidence(file="agent/b.py", line=5, snippet="import agent.a")],
    )
    blob = _minimal_blob(cycles=[cycle])
    result = summarize_graph_findings(blob)
    assert "cyc-1" in result
    assert "module:agent.a" in result
    assert "module:agent.b" in result


def test_cycle_appears_even_with_no_hotspots():
    """Cycles are correctness issues — hotspot gate does not apply."""
    cycle = DependencyCycle(
        id="cyc-frozen",
        kind="import",
        members=["module:cold.x", "module:cold.y"],
        closing_edges=[],
    )
    blob = _minimal_blob(cycles=[cycle])
    result = summarize_graph_findings(blob)
    assert "cyc-frozen" in result


# ---------------------------------------------------------------------------
# Dead code — always reported
# ---------------------------------------------------------------------------


def test_dead_code_always_appears():
    """Dead code findings appear regardless of hotspot status."""
    blob = _minimal_blob(
        dead_code=[
            DeadCodeFinding(
                kind="unused_export",
                target="agent/old.py::_legacy_helper",
                file="agent/old.py",
                reason="No callers found",
            ),
        ]
    )
    result = summarize_graph_findings(blob)
    assert "_legacy_helper" in result
    assert "unused_export" in result


def test_dead_code_grouped_by_kind():
    """Multiple dead-code findings are grouped by kind in the output."""
    blob = _minimal_blob(
        dead_code=[
            DeadCodeFinding(
                kind="unused_export",
                target="agent/a.py::fn_a",
                file="agent/a.py",
                reason="No callers",
            ),
            DeadCodeFinding(
                kind="unused_file",
                target="file:agent/b.py",
                file="agent/b.py",
                reason="No imports",
            ),
        ]
    )
    result = summarize_graph_findings(blob)
    assert "unused_export" in result
    assert "unused_file" in result
    assert "fn_a" in result


# ---------------------------------------------------------------------------
# Health summary
# ---------------------------------------------------------------------------


def test_health_summary_appears_when_blob_has_health():
    blob = _minimal_blob(
        health=RepoHealth(score=55.0, clone_count=3, cycle_count=1, dead_count=2, hotspot_count=4),
        file_health=[
            FileHealth(file="agent/bad.py", maintainability_index=25.0, band="poor"),
        ],
    )
    result = summarize_graph_findings(blob)
    assert "55.0" in result
    assert "poor" in result
    assert "agent/bad.py" in result


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_output_is_deterministic():
    """Two calls with the same blob must produce identical output."""
    blob = _minimal_blob(
        hotspots=[_hotspot("agent/foo.py", score=65)],
        nodes=[
            _func_node("agent/foo.py::func_a", "agent/foo.py", cyclomatic=22),
            _func_node("agent/foo.py::func_b", "agent/foo.py", cyclomatic=20),
        ],
        cycles=[
            DependencyCycle(
                id="cyc-det",
                kind="import",
                members=["module:x", "module:y"],
                closing_edges=[],
            )
        ],
        dead_code=[
            DeadCodeFinding(kind="unused_export", target="x::foo", file="x.py", reason="no callers")
        ],
    )
    first = summarize_graph_findings(blob)
    second = summarize_graph_findings(blob)
    assert first == second


# ---------------------------------------------------------------------------
# Hotspot section itself
# ---------------------------------------------------------------------------


def test_hotspot_section_lists_qualifying_files():
    blob = _minimal_blob(
        hotspots=[
            _hotspot("agent/foo.py", score=75),
            _hotspot("agent/bar.py", score=25),  # below threshold
        ],
    )
    result = summarize_graph_findings(blob, hotspot_min_score=50.0)
    assert "agent/foo.py" in result
    # agent/bar.py is below threshold so it doesn't appear as a hotspot
    assert "agent/bar.py" not in result


def test_top_10_hotspots_limit():
    """Only top 10 hotspots are listed even if more qualify."""
    hotspots = [_hotspot(f"file_{i}.py", score=60 + i) for i in range(15)]
    blob = _minimal_blob(hotspots=hotspots)
    result = summarize_graph_findings(blob)
    # At most 10 files listed; the blob is pre-sorted score DESC so file_14..file_5 appear
    count = sum(1 for i in range(15) if f"file_{i}.py" in result)
    assert count <= 10
