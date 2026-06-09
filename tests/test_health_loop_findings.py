"""Phase 1 — pure finding ranking / identity / filtering."""
from __future__ import annotations

from agent.health_loop.findings import (
    CATEGORY_WEIGHTS,
    HealthFinding,
)


def test_health_finding_is_frozen_and_carries_core_fields():
    f = HealthFinding(
        finding_hash="abc123",
        category="dead_code",
        title="unused export api/routes.py::helper",
        files=["api/routes.py"],
        severity=1.0,
    )
    assert f.finding_hash == "abc123"
    assert f.category == "dead_code"
    assert f.files == ["api/routes.py"]


def test_category_weights_match_composite_health_weighting():
    assert CATEGORY_WEIGHTS == {
        "poor_file": 0.30,
        "dead_code": 0.25,
        "clone": 0.20,
        "hotspot": 0.15,
        "cycle": 0.10,
    }


from agent.health_loop.findings import finding_hash


def test_finding_hash_is_stable_and_order_independent():
    h1 = finding_hash("cycle", ["a.py::x", "b.py::y"])
    h2 = finding_hash("cycle", ["b.py::y", "a.py::x"])
    assert h1 == h2
    assert len(h1) == 16


def test_finding_hash_distinguishes_category_and_payload():
    assert finding_hash("dead_code", ["api/routes.py::helper"]) != finding_hash(
        "dead_code", ["api/routes.py::other"]
    )
    assert finding_hash("dead_code", ["x"]) != finding_hash("hotspot", ["x"])


from datetime import datetime

from shared.types import (
    CloneGroup,
    CloneInstance,
    DeadCodeFinding,
    DependencyCycle,
    FileHealth,
    Hotspot,
    RepoGraphBlob,
)

from agent.health_loop.findings import extract_findings


def _blob(**kw) -> RepoGraphBlob:
    base = dict(
        commit_sha="deadbeef",
        generated_at=datetime(2026, 1, 1),
        analyser_version="test",
        areas=[],
        nodes=[], edges=[], dead_code=[], cycles=[], clones=[],
        hotspots=[], file_health=[], health=None,
    )
    base.update(kw)
    return RepoGraphBlob(**base)


def test_extract_covers_every_category():
    blob = _blob(
        dead_code=[DeadCodeFinding(kind="unused_export", target="a.py::h", file="a.py", reason="never imported")],
        cycles=[DependencyCycle(id="c1", kind="import", members=["a.py", "b.py"], closing_edges=[])],
        clones=[CloneGroup(id="g1", token_len=120, mode="strict", instances=[
            CloneInstance(node_id="a.py::f", file="a.py", line_start=1, line_end=9),
            CloneInstance(node_id="b.py::g", file="b.py", line_start=1, line_end=9),
        ], family_id=None)],
        hotspots=[Hotspot(file="a.py", churn=5.0, complexity_density=0.4, score=80.0, trend="accelerating")],
        file_health=[
            FileHealth(file="a.py", maintainability_index=20.0, band="poor"),
            FileHealth(file="ok.py", maintainability_index=90.0, band="good"),
        ],
    )
    found = extract_findings(blob)
    cats = {f.category for f in found}
    assert cats == {"dead_code", "cycle", "clone", "hotspot", "poor_file"}
    assert all("ok.py" not in f.files for f in found if f.category == "poor_file")


def test_extract_severity_reflects_magnitude():
    blob = _blob(file_health=[
        FileHealth(file="worse.py", maintainability_index=10.0, band="poor"),
        FileHealth(file="bad.py", maintainability_index=35.0, band="poor"),
    ])
    found = {f.files[0]: f for f in extract_findings(blob)}
    assert found["worse.py"].severity > found["bad.py"].severity
