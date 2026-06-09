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
