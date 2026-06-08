"""Unit tests for agent.graph_analyzer.health.compute_health.

All tests use hand-built blobs with synthetic nodes/edges/dead_code — no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent.graph_analyzer.health import compute_health
from shared.types import (
    CloneGroup,
    CloneInstance,
    DeadCodeFinding,
    DependencyCycle,
    Edge,
    EdgeEvidence,
    Node,
    RepoGraphBlob,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _blob(
    *,
    nodes: list[Node] | None = None,
    edges: list[Edge] | None = None,
    dead_code: list[DeadCodeFinding] | None = None,
    clones=None,
    cycles=None,
    hotspots=None,
) -> RepoGraphBlob:
    return RepoGraphBlob(
        commit_sha="test",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        analyser_version="phase13-health-0.13.0",
        areas=[],
        nodes=nodes or [],
        edges=edges or [],
        dead_code=dead_code or [],
        clones=clones or [],
        cycles=cycles or [],
        hotspots=hotspots or [],
    )


def _fn_node(
    file: str,
    name: str,
    area: str = "area1",
    cyclomatic: int | None = None,
) -> Node:
    return Node(
        id=f"{file}::{name}",
        kind="function",
        label=name,
        file=file,
        area=area,
        cyclomatic=cyclomatic,
    )


def _class_node(file: str, name: str, area: str = "area1") -> Node:
    return Node(
        id=f"{file}::{name}",
        kind="class",
        label=name,
        file=file,
        area=area,
    )


def _edge(src: str, tgt: str, src_file: str = "src.py") -> Edge:
    return Edge(
        source=src,
        target=tgt,
        kind="calls",
        evidence=EdgeEvidence(file=src_file, line=1, snippet="x()"),
        source_kind="ast",
    )


# ---------------------------------------------------------------------------
# Test: clean simple file → MI near 100, band "good"
# ---------------------------------------------------------------------------


def test_clean_file_high_mi():
    """A file with low complexity, no dead code, no fan-out → MI near 100."""
    nodes = [_fn_node("a.py", "foo", cyclomatic=1)]
    blob = _blob(nodes=nodes)
    file_loc = {"a.py": 10}
    file_cyc = {"a.py": 1}

    fh_list, _rh = compute_health(blob, file_loc, file_cyc)
    assert len(fh_list) == 1
    fh = fh_list[0]
    # density = 1/10 = 0.1 → penalty = 0.1*30 = 3; no dead, no fan-out → MI = 97
    assert fh.maintainability_index == pytest.approx(97.0)
    assert fh.band == "good"


# ---------------------------------------------------------------------------
# Test: dense file → MI lowered by complexity_density * 30
# ---------------------------------------------------------------------------


def test_dense_file_mi_lowered():
    """High cyclomatic/loc density should reduce MI by density*30 exactly."""
    nodes = [_fn_node("b.py", "complex_fn", cyclomatic=20)]
    blob = _blob(nodes=nodes)
    file_loc = {"b.py": 50}  # density = 20/50 = 0.4
    file_cyc = {"b.py": 20}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    fh = fh_list[0]
    # MI = 100 - 0.4*30 = 100 - 12 = 88
    assert fh.maintainability_index == pytest.approx(88.0)
    assert fh.band == "good"


# ---------------------------------------------------------------------------
# Test: unused_export → dead_code_ratio lowers MI
# ---------------------------------------------------------------------------


def test_unused_export_lowers_mi():
    """One unused_export out of 2 function nodes → ratio=0.5 → penalty 10."""
    nodes = [
        _fn_node("c.py", "used_fn", cyclomatic=1),
        _fn_node("c.py", "unused_fn", cyclomatic=1),
    ]
    dead = [
        DeadCodeFinding(
            kind="unused_export",
            target="c.py::unused_fn",
            file="c.py",
            reason="never called",
        )
    ]
    blob = _blob(nodes=nodes, dead_code=dead)
    file_loc = {"c.py": 20}
    file_cyc = {"c.py": 2}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    fh = fh_list[0]
    # density=2/20=0.1 → complexity term=3; dead ratio=1/2=0.5 → dead term=10; fan-out=0
    # MI = 100 - 3 - 10 = 87
    assert fh.maintainability_index == pytest.approx(87.0)


# ---------------------------------------------------------------------------
# Test: unused_file → ratio capped at 1.0 → -20 from that term
# ---------------------------------------------------------------------------


def test_unused_file_caps_dead_ratio():
    """unused_file finding forces ratio=1.0 regardless of node count."""
    nodes = [_fn_node("d.py", "orphan_fn", cyclomatic=1)]
    dead = [
        DeadCodeFinding(
            kind="unused_file",
            target="file:d.py",
            file="d.py",
            reason="nothing imports it",
        )
    ]
    blob = _blob(nodes=nodes, dead_code=dead)
    file_loc = {"d.py": 10}
    file_cyc = {"d.py": 1}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    fh = fh_list[0]
    # density=1/10=0.1 → 3; dead ratio=1.0 → 20; fan-out=0
    # MI = 100 - 3 - 20 = 77
    assert fh.maintainability_index == pytest.approx(77.0)
    assert fh.band == "good"


# ---------------------------------------------------------------------------
# Test: fan-out penalty = min(20, 2*N)
# ---------------------------------------------------------------------------


def test_fan_out_penalty_small_n():
    """3 cross-area outgoing edges → penalty = min(20, 6) = 6."""
    nodes = [
        _fn_node("e.py", "caller", area="area1", cyclomatic=1),
        _fn_node("other.py", "callee1", area="area2", cyclomatic=1),
        _fn_node("other.py", "callee2", area="area2", cyclomatic=1),
        _fn_node("other.py", "callee3", area="area2", cyclomatic=1),
    ]
    edges = [
        _edge("e.py::caller", "other.py::callee1", src_file="e.py"),
        _edge("e.py::caller", "other.py::callee2", src_file="e.py"),
        _edge("e.py::caller", "other.py::callee3", src_file="e.py"),
    ]
    blob = _blob(nodes=nodes, edges=edges)
    file_loc = {"e.py": 10, "other.py": 10}
    file_cyc = {"e.py": 1, "other.py": 3}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    fh_e = next(fh for fh in fh_list if fh.file == "e.py")
    # density=1/10=0.1 → 3; dead=0; fan-out penalty=min(20, 2*3)=6
    # MI = 100 - 3 - 0 - 6 = 91
    assert fh_e.maintainability_index == pytest.approx(91.0)


def test_fan_out_penalty_caps_at_20():
    """15 cross-area outgoing edges → penalty = min(20, 30) = 20."""
    src_node = _fn_node("f.py", "big_caller", area="area1", cyclomatic=1)
    tgt_nodes = [_fn_node("other.py", f"fn{i}", area="area2") for i in range(15)]
    edges = [_edge("f.py::big_caller", f"other.py::fn{i}", src_file="f.py") for i in range(15)]
    blob = _blob(nodes=[src_node, *tgt_nodes], edges=edges)
    file_loc = {"f.py": 10, "other.py": 50}
    file_cyc = {"f.py": 1, "other.py": 0}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    fh_f = next(fh for fh in fh_list if fh.file == "f.py")
    # density=1/10=0.1 → 3; dead=0; fan-out=min(20,30)=20
    # MI = 100 - 3 - 0 - 20 = 77
    assert fh_f.maintainability_index == pytest.approx(77.0)


# ---------------------------------------------------------------------------
# Test: MI clamped to [0, 100] for pathological file
# ---------------------------------------------------------------------------


def test_mi_clamps_to_zero():
    """Pathological file: very high density + all dead + max fan-out → MI 0."""
    src_node = _fn_node("g.py", "monster", area="area1", cyclomatic=500)
    tgt_nodes = [_fn_node("other.py", f"fn{i}", area="area2") for i in range(20)]
    edges = [_edge("g.py::monster", f"other.py::fn{i}", src_file="g.py") for i in range(20)]
    dead = [
        DeadCodeFinding(
            kind="unused_export",
            target="g.py::monster",
            file="g.py",
            reason="never called",
        )
    ]
    blob = _blob(nodes=[src_node, *tgt_nodes], edges=edges, dead_code=dead)
    file_loc = {"g.py": 10, "other.py": 50}
    file_cyc = {"g.py": 500, "other.py": 0}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    fh_g = next(fh for fh in fh_list if fh.file == "g.py")
    assert fh_g.maintainability_index == 0.0
    assert fh_g.band == "poor"


# ---------------------------------------------------------------------------
# Test: band boundaries
# ---------------------------------------------------------------------------


def test_band_exactly_70_is_good():
    """MI exactly 70 maps to band 'good'."""
    # density=1.0 → 30; no dead, no fan-out → MI = 100 - 30 = 70
    nodes = [_fn_node("h.py", "fn", cyclomatic=10)]
    blob = _blob(nodes=nodes)
    file_loc = {"h.py": 10}  # density = 10/10 = 1.0
    file_cyc = {"h.py": 10}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    fh = fh_list[0]
    assert fh.maintainability_index == pytest.approx(70.0)
    assert fh.band == "good"


def test_band_exactly_40_is_moderate():
    """MI exactly 40 maps to band 'moderate'."""
    # density=2.0 → 60; no dead, no fan-out → MI = 40
    nodes = [_fn_node("i.py", "fn", cyclomatic=20)]
    blob = _blob(nodes=nodes)
    file_loc = {"i.py": 10}  # density = 20/10 = 2.0
    file_cyc = {"i.py": 20}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    fh = fh_list[0]
    assert fh.maintainability_index == pytest.approx(40.0)
    assert fh.band == "moderate"


def test_band_just_below_40_is_poor():
    """MI just below 40 maps to band 'poor'."""
    # density ~ 2.01 → just over 60 → MI just below 40
    nodes = [_fn_node("j.py", "fn", cyclomatic=21)]
    blob = _blob(nodes=nodes)
    file_loc = {"j.py": 10}
    file_cyc = {"j.py": 21}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    fh = fh_list[0]
    assert fh.maintainability_index < 40.0
    assert fh.band == "poor"


# ---------------------------------------------------------------------------
# Test: maintainability sub-score = LOC-weighted mean of per-file MI
# ---------------------------------------------------------------------------


def test_maintainability_subscore_loc_weighted_mean():
    """The maintainability sub-score is the exact LOC-weighted MI mean.

    (The composite ``score`` is a weighted blend of all sub-scores; the
    LOC-weighted mean now lives on ``maintainability``.)"""
    nodes = [
        _fn_node("file1.py", "fn", area="area1", cyclomatic=1),
        _fn_node("file2.py", "fn", area="area1", cyclomatic=10),
    ]
    blob = _blob(nodes=nodes)
    # file1: density=1/10=0.1 → MI=97; file2: density=10/20=0.5 → MI=85
    file_loc = {"file1.py": 10, "file2.py": 20}
    file_cyc = {"file1.py": 1, "file2.py": 10}

    _fh_list, rh = compute_health(blob, file_loc, file_cyc)
    # Weighted = (97*10 + 85*20) / 30 = 2670/30 = 89.0
    assert rh.maintainability == pytest.approx(89.0)
    # No findings → other sub-scores are 100, so composite > maintainability.
    assert rh.score == pytest.approx(
        0.30 * 89.0 + 0.25 * 100 + 0.20 * 100 + 0.15 * 100 + 0.10 * 100
    )


def test_repo_health_counts():
    """RepoHealth counts match len() of blob list fields."""
    from shared.types import CloneGroup, CloneInstance, DependencyCycle, Hotspot

    nodes = [_fn_node("z.py", "fn", cyclomatic=1)]
    clones = [
        CloneGroup(
            id="c1",
            token_len=10,
            mode="strict",
            instances=[
                CloneInstance(node_id="z.py::fn", file="z.py", line_start=1, line_end=5),
                CloneInstance(node_id="z.py::fn2", file="z.py", line_start=10, line_end=14),
            ],
        )
    ]
    cycles = [
        DependencyCycle(
            id="cy1",
            kind="import",
            members=["module:z"],
            closing_edges=[],
        )
    ]
    dead = [DeadCodeFinding(kind="unused_export", target="z.py::fn", file="z.py", reason="test")]
    hotspots = [Hotspot(file="z.py", churn=1.0, complexity_density=0.5, score=50.0, trend="stable")]
    blob = _blob(nodes=nodes, clones=clones, cycles=cycles, dead_code=dead, hotspots=hotspots)
    file_loc = {"z.py": 10}
    file_cyc = {"z.py": 1}

    _, rh = compute_health(blob, file_loc, file_cyc)
    assert rh.clone_count == 1
    assert rh.cycle_count == 1
    assert rh.dead_count == 1
    assert rh.hotspot_count == 1


# ---------------------------------------------------------------------------
# Test: determinism + sort by file
# ---------------------------------------------------------------------------


def test_file_health_sorted_by_file():
    """file_health list is always sorted by file path ascending."""
    nodes = [
        _fn_node("z_last.py", "fn1", cyclomatic=1),
        _fn_node("a_first.py", "fn2", cyclomatic=1),
        _fn_node("m_middle.py", "fn3", cyclomatic=1),
    ]
    blob = _blob(nodes=nodes)
    file_loc = {"z_last.py": 10, "a_first.py": 10, "m_middle.py": 10}
    file_cyc = {"z_last.py": 1, "a_first.py": 1, "m_middle.py": 1}

    fh_list, _ = compute_health(blob, file_loc, file_cyc)
    files = [fh.file for fh in fh_list]
    assert files == sorted(files)
    assert files[0] == "a_first.py"


def test_compute_health_is_deterministic():
    """Two calls with identical input produce identical output."""
    nodes = [
        _fn_node("x.py", "fn1", cyclomatic=3),
        _fn_node("y.py", "fn2", cyclomatic=7),
    ]
    blob = _blob(nodes=nodes)
    file_loc = {"x.py": 20, "y.py": 30}
    file_cyc = {"x.py": 3, "y.py": 7}

    fh1, rh1 = compute_health(blob, file_loc, file_cyc)
    fh2, rh2 = compute_health(blob, file_loc, file_cyc)
    assert [fh.file for fh in fh1] == [fh.file for fh in fh2]
    assert [fh.maintainability_index for fh in fh1] == [fh.maintainability_index for fh in fh2]
    assert rh1.score == rh2.score


# ---------------------------------------------------------------------------
# Test: empty blob → score 100.0
# ---------------------------------------------------------------------------


def test_empty_blob_returns_score_100():
    """With no function/class nodes, RepoHealth.score defaults to 100.0."""
    blob = _blob()
    fh_list, rh = compute_health(blob, {}, {})
    assert fh_list == []
    assert rh.score == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Composite + per-dimension sub-scores
# ---------------------------------------------------------------------------


class TestSubScores:
    def _file(self, path: str, area: str = "area1") -> Node:
        return Node(id=f"file:{path}", kind="file", label=path, file=path, area=area)

    def test_clean_repo_all_subscores_high(self):
        blob = _blob(nodes=[self._file("a.py"), _fn_node("a.py", "f", cyclomatic=1)])
        _, rh = compute_health(blob, {"a.py": 20}, {"a.py": 1})
        assert rh.maintainability >= 90
        assert rh.duplication == 100
        assert rh.dead_code == 100
        assert rh.cycles == 100
        assert rh.coupling == 100
        assert rh.score >= 90

    def test_heavy_duplication_drops_subscore_and_composite(self):
        blob = _blob(
            nodes=[self._file("a.py"), _fn_node("a.py", "f", cyclomatic=1)],
            clones=[
                CloneGroup(
                    id="g1",
                    token_len=100,
                    mode="strict",
                    instances=[
                        CloneInstance(node_id="a.py::f", file="a.py", line_start=1, line_end=18)
                    ],
                )
            ],
        )
        _, rh = compute_health(blob, {"a.py": 20}, {"a.py": 1})
        assert rh.duplication < 30  # 18 of 20 lines cloned
        assert rh.score < 90

    def test_dead_code_subscore_excludes_test_only(self):
        blob = _blob(
            nodes=[
                self._file("a.py"),
                _fn_node("a.py", "f1", cyclomatic=1),
                _fn_node("a.py", "f2", cyclomatic=1),
            ],
            dead_code=[
                DeadCodeFinding(
                    kind="unused_export",
                    target="a.py::f1",
                    file="a.py",
                    reason="exported but no external caller or subclass",
                ),
                DeadCodeFinding(
                    kind="unused_export",
                    target="a.py::f2",
                    file="a.py",
                    reason="referenced only by tests",
                ),
            ],
        )
        _, rh = compute_health(blob, {"a.py": 20}, {"a.py": 2})
        # Only f1 is real dead; f2 is test-only and excluded. denom = 2 fns + 1 file = 3.
        assert 60.0 < rh.dead_code < 75.0

    def test_cycles_subscore_drops(self):
        blob = _blob(
            nodes=[_fn_node("a.py", "f1", cyclomatic=1), _fn_node("a.py", "f2", cyclomatic=1)],
            cycles=[
                DependencyCycle(
                    id="c1", kind="import", members=["a.py::f1", "a.py::f2"], closing_edges=[]
                )
            ],
        )
        _, rh = compute_health(blob, {"a.py": 20}, {"a.py": 2})
        assert rh.cycles < 50.0  # both nodes tangled in a cycle

    def test_composite_equals_weighted_sum_of_subscores(self):
        blob = _blob(
            nodes=[self._file("a.py"), _fn_node("a.py", "f", cyclomatic=8)],
            clones=[
                CloneGroup(
                    id="g1",
                    token_len=50,
                    mode="strict",
                    instances=[
                        CloneInstance(node_id="a.py::f", file="a.py", line_start=1, line_end=5)
                    ],
                )
            ],
        )
        _, rh = compute_health(blob, {"a.py": 20}, {"a.py": 8})
        expected = (
            0.30 * rh.maintainability
            + 0.25 * rh.dead_code
            + 0.20 * rh.duplication
            + 0.15 * rh.coupling
            + 0.10 * rh.cycles
        )
        assert abs(rh.score - expected) < 0.01
