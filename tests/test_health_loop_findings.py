"""Phase 1 — pure finding ranking / identity / filtering."""

from __future__ import annotations

from datetime import datetime

from agent.health_loop.findings import (
    CATEGORY_WEIGHTS,
    HealthFinding,
    extract_findings,
    finding_hash,
    rank_findings,
    select_batch,
)
from shared.types import (
    CloneGroup,
    CloneInstance,
    DeadCodeFinding,
    DependencyCycle,
    FileHealth,
    Hotspot,
    RepoGraphBlob,
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


def _blob(**kw) -> RepoGraphBlob:
    base = dict(
        commit_sha="deadbeef",
        generated_at=datetime(2026, 1, 1),
        analyser_version="test",
        areas=[],
        nodes=[],
        edges=[],
        dead_code=[],
        cycles=[],
        clones=[],
        hotspots=[],
        file_health=[],
        health=None,
    )
    base.update(kw)
    return RepoGraphBlob(**base)


def test_extract_covers_every_category():
    blob = _blob(
        dead_code=[
            DeadCodeFinding(
                kind="unused_export", target="a.py::h", file="a.py", reason="never imported"
            )
        ],
        cycles=[
            DependencyCycle(
                id="c1", kind="import", members=["file:a.py", "file:b.py"], closing_edges=[]
            )
        ],
        clones=[
            CloneGroup(
                id="g1",
                token_len=120,
                mode="strict",
                instances=[
                    CloneInstance(node_id="a.py::f", file="a.py", line_start=1, line_end=9),
                    CloneInstance(node_id="b.py::g", file="b.py", line_start=1, line_end=9),
                ],
                family_id=None,
            )
        ],
        hotspots=[
            Hotspot(
                file="a.py", churn=5.0, complexity_density=0.4, score=80.0, trend="accelerating"
            )
        ],
        file_health=[
            FileHealth(file="a.py", maintainability_index=20.0, band="poor"),
            FileHealth(file="ok.py", maintainability_index=90.0, band="good"),
        ],
    )
    found = extract_findings(blob)
    cats = {f.category for f in found}
    assert cats == {"dead_code", "cycle", "clone", "hotspot", "poor_file"}
    assert all("ok.py" not in f.files for f in found if f.category == "poor_file")

    # Cycle members are import-graph vertex ids in ``file:<rel_path>`` form;
    # the finding must emit bare paths (no leaked ``file:`` prefix).
    cycle = next(f for f in found if f.category == "cycle")
    assert cycle.files == ["a.py", "b.py"]
    assert all(not p.startswith("file:") for p in cycle.files)


def test_clone_finding_hash_stable_across_line_shifts():
    # Same instances / node_ids, different line numbers (an edit shifted the
    # clone down the file) must produce the SAME finding_hash — otherwise a
    # suppressed clone would reappear after any unrelated edit above it.
    def _clone(start, end):
        return CloneGroup(
            id="g1",
            token_len=120,
            mode="strict",
            instances=[
                CloneInstance(node_id="a.py::f", file="a.py", line_start=start, line_end=end),
                CloneInstance(node_id="b.py::g", file="b.py", line_start=start, line_end=end),
            ],
            family_id=None,
        )

    h1 = extract_findings(_blob(clones=[_clone(1, 9)]))[0].finding_hash
    h2 = extract_findings(_blob(clones=[_clone(40, 48)]))[0].finding_hash
    assert h1 == h2


def test_extract_excludes_dependency_findings():
    # The loop must never auto-edit pyproject deps — static analysis can't see
    # runtime-only deps (pytest/uvicorn/asyncpg), so every dependency finding
    # is dropped.
    blob = _blob(
        dead_code=[
            DeadCodeFinding(kind="unused_dependency", target="pytest", file=None, reason="x"),
            DeadCodeFinding(kind="undeclared_dependency", target="bcrypt", file=None, reason="x"),
        ]
    )
    assert extract_findings(blob) == []


def test_extract_excludes_nextjs_route_entry_files():
    # Next.js app-router special files are route entry points — nothing imports
    # them, so the analyzer mis-flags them as unused_file. Deleting them removes
    # routes / the UI.
    specials = [
        "web-next/app/(app)/settings/page.tsx",
        "web-next/app/(app)/layout.tsx",
        "web-next/app/layout.tsx",
        "web-next/app/page.tsx",
        "web-next/app/(public)/verify/[token]/page.tsx",
        "web-next/app/api/health/route.ts",
        "web-next/middleware.ts",
    ]
    blob = _blob(
        dead_code=[
            DeadCodeFinding(kind="unused_file", target=f"file:{p}", file=p, reason="no importer")
            for p in specials
        ]
    )
    assert extract_findings(blob) == []


def test_extract_excludes_entry_dirs_and_basenames():
    paths = [
        "migrations/env.py",
        "migrations/versions/057_x.py",
        "scripts/gen_ts_types.py",
        "eval/providers/agent_provider.py",
        "run.py",
        "app.py",
        "conftest.py",
    ]
    blob = _blob(
        dead_code=[
            DeadCodeFinding(kind="unused_file", target=f"file:{p}", file=p, reason="no importer")
            for p in paths
        ]
    )
    assert extract_findings(blob) == []


def test_extract_excludes_dispatch_handlers():
    # Functions registered on the event bus / run as background loops look
    # unused to static analysis. Exclude handler/loop-shaped export names.
    handlers = [
        "agent/lifecycle/coding.py::handle_coding",
        "agent/lifecycle/coding.py::handle",
        "run.py::on_task_created",
        "agent/improvement_agent.py::run_architecture_loop",
        "x/y.py::pr_merge_poller",  # not matched by name — see below
    ]
    blob = _blob(
        dead_code=[
            DeadCodeFinding(kind="unused_export", target=t, file=t.split("::")[0], reason="x")
            for t in handlers
        ]
    )
    kept = {f.title for f in extract_findings(blob)}
    # handle / handle_* / on_* / run_*_loop excluded; the poller (no matching
    # affix) survives and would be judged by the coder + gates.
    assert all("handle_coding" not in t for t in kept)
    assert all("::handle " not in t and "::handle —" not in t for t in kept)
    assert all("on_task_created" not in t for t in kept)
    assert all("run_architecture_loop" not in t for t in kept)


def test_extract_keeps_genuine_orphan_export_and_file():
    blob = _blob(
        dead_code=[
            DeadCodeFinding(
                kind="unused_export",
                target="web-next/lib/usage.ts::formatBytes",
                file="web-next/lib/usage.ts",
                reason="exported but no importer",
            ),
            DeadCodeFinding(
                kind="unused_file",
                target="file:web-next/components/code-graph/orphan-widget.tsx",
                file="web-next/components/code-graph/orphan-widget.tsx",
                reason="no module imports this file",
            ),
        ]
    )
    titles = {f.title for f in extract_findings(blob)}
    assert any("formatBytes" in t for t in titles)
    assert any("orphan-widget" in t for t in titles)


def test_extract_severity_reflects_magnitude():
    blob = _blob(
        file_health=[
            FileHealth(file="worse.py", maintainability_index=10.0, band="poor"),
            FileHealth(file="bad.py", maintainability_index=35.0, band="poor"),
        ]
    )
    found = {f.files[0]: f for f in extract_findings(blob)}
    assert found["worse.py"].severity > found["bad.py"].severity


def test_rank_orders_by_category_weight_then_severity():
    blob = _blob(
        cycles=[
            DependencyCycle(id="c1", kind="import", members=["a.py", "b.py"], closing_edges=[])
        ],
        file_health=[FileHealth(file="a.py", maintainability_index=10.0, band="poor")],
    )
    ranked = rank_findings(blob)
    assert ranked[0].category == "poor_file"
    assert ranked[-1].category == "cycle"


def test_rank_is_deterministic():
    blob = _blob(
        dead_code=[
            DeadCodeFinding(kind="unused_export", target=f"a.py::h{i}", file="a.py", reason="x")
            for i in range(5)
        ]
    )
    assert [f.finding_hash for f in rank_findings(blob)] == [
        f.finding_hash for f in rank_findings(blob)
    ]


def test_select_batch_excludes_suppressed_and_in_flight_and_caps_at_n():
    blob = _blob(
        dead_code=[
            DeadCodeFinding(kind="unused_export", target=f"a.py::h{i}", file="a.py", reason="x")
            for i in range(10)
        ]
    )
    ranked = rank_findings(blob)
    suppressed = {ranked[0].finding_hash}
    in_flight = {ranked[1].finding_hash}

    batch = select_batch(blob, suppressed=suppressed, in_flight=in_flight, batch_size=3)

    hashes = {f.finding_hash for f in batch}
    assert len(batch) == 3
    assert ranked[0].finding_hash not in hashes
    assert ranked[1].finding_hash not in hashes


def test_select_batch_empty_when_all_filtered():
    blob = _blob(
        dead_code=[DeadCodeFinding(kind="unused_export", target="a.py::h", file="a.py", reason="x")]
    )
    only = rank_findings(blob)[0].finding_hash
    assert select_batch(blob, suppressed={only}, in_flight=set(), batch_size=5) == []
