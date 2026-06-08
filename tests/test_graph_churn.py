"""Unit tests for agent.graph_analyzer.churn.compute_hotspots (pure, no I/O).

All tests pass explicit timestamps, loc, and cyclomatic data plus a fixed
reference_ts, so results are fully deterministic and require no git, no
filesystem, and no async runtime.
"""

from __future__ import annotations

import pytest

from agent.graph_analyzer.churn import compute_hotspots

# A fixed reference timestamp (arbitrary but stable).
_REF = 1_700_000_000  # 2023-11-14 22:13:20 UTC


def _ts_ago(days: float) -> int:
    """Return a Unix timestamp ``days`` days before ``_REF``."""
    return int(_REF - days * 86400)


# ---------------------------------------------------------------------------
# Basic ranking
# ---------------------------------------------------------------------------


def test_high_churn_high_density_scores_highest() -> None:
    """A file with high churn AND high density outranks files with only one."""
    # high_churn_high_density: many recent commits, high cyclomatic/loc
    # high_churn_low_density:  same commits, low cyclomatic/loc
    # low_churn_high_density:  few old commits, high cyclomatic/loc
    ref = _REF
    commits_many_recent = [_ts_ago(1), _ts_ago(2), _ts_ago(5), _ts_ago(10)]
    commits_few_old = [_ts_ago(170)]

    file_commit_timestamps = {
        "high_high.py": commits_many_recent,
        "high_low.py": commits_many_recent,
        "low_high.py": commits_few_old,
    }
    file_loc = {
        "high_high.py": 200,
        "high_low.py": 200,
        "low_high.py": 200,
    }
    file_cyclomatic_total = {
        "high_high.py": 100,  # density = 0.5
        "high_low.py": 4,  # density = 0.02
        "low_high.py": 100,  # density = 0.5
    }

    results = compute_hotspots(
        file_commit_timestamps,
        file_loc,
        file_cyclomatic_total,
        reference_ts=ref,
    )

    assert len(results) == 3
    by_file = {h.file: h for h in results}

    # high_high must be ranked first (score == 100 since it has max churn AND max density)
    assert results[0].file == "high_high.py"
    assert results[0].score == pytest.approx(100.0)

    # high_low scores less than high_high (same churn, lower density)
    assert by_file["high_low.py"].score < by_file["high_high.py"].score

    # low_high scores less than high_high (same density, lower churn)
    assert by_file["low_high.py"].score < by_file["high_high.py"].score


# ---------------------------------------------------------------------------
# Exclusion: zero commits in window
# ---------------------------------------------------------------------------


def test_file_with_density_but_zero_window_commits_excluded() -> None:
    """A file with cyclomatic/loc defined but no commits in window → not in output."""
    ref = _REF
    file_commit_timestamps: dict[str, list[int]] = {
        "active.py": [_ts_ago(10)],
        # "inactive.py" has NO commits in the window at all
    }
    file_loc = {"active.py": 100, "inactive.py": 100}
    file_cyclomatic_total = {"active.py": 10, "inactive.py": 10}

    results = compute_hotspots(
        file_commit_timestamps,
        file_loc,
        file_cyclomatic_total,
        reference_ts=ref,
    )

    files = {h.file for h in results}
    assert "inactive.py" not in files
    assert "active.py" in files


def test_file_with_only_out_of_window_commits_excluded() -> None:
    """Commits that pre-date the window start are NOT counted."""
    ref = _REF
    # window_days default is 180; commits at 200 and 300 days are outside
    file_commit_timestamps = {
        "stale.py": [_ts_ago(200), _ts_ago(300)],
        "fresh.py": [_ts_ago(10)],
    }
    file_loc = {"stale.py": 100, "fresh.py": 100}
    file_cyclomatic_total = {"stale.py": 20, "fresh.py": 20}

    results = compute_hotspots(
        file_commit_timestamps,
        file_loc,
        file_cyclomatic_total,
        reference_ts=ref,
    )

    files = {h.file for h in results}
    assert "stale.py" not in files
    assert "fresh.py" in files


# ---------------------------------------------------------------------------
# Exclusion: zero density (churn but no cyclomatic)
# ---------------------------------------------------------------------------


def test_file_with_churn_but_zero_density_score_is_zero_or_excluded() -> None:
    """A file with commits but no cyclomatic total → either excluded or score 0."""
    ref = _REF
    file_commit_timestamps = {
        "complex.py": [_ts_ago(5)],
        "simple.py": [_ts_ago(5)],
    }
    file_loc = {"complex.py": 100, "simple.py": 100}
    file_cyclomatic_total = {
        "complex.py": 30,
        # "simple.py" has no entry → density 0 → excluded from output
    }

    results = compute_hotspots(
        file_commit_timestamps,
        file_loc,
        file_cyclomatic_total,
        reference_ts=ref,
    )

    files = {h.file for h in results}
    assert "simple.py" not in files, "zero-density file must be excluded"
    assert "complex.py" in files


def test_file_with_cyclomatic_but_zero_loc_excluded() -> None:
    """loc == 0 → density is 0/undefined → file excluded."""
    ref = _REF
    file_commit_timestamps = {
        "empty.py": [_ts_ago(5)],
        "normal.py": [_ts_ago(5)],
    }
    file_loc = {"empty.py": 0, "normal.py": 100}
    file_cyclomatic_total = {"empty.py": 10, "normal.py": 10}

    results = compute_hotspots(
        file_commit_timestamps,
        file_loc,
        file_cyclomatic_total,
        reference_ts=ref,
    )

    files = {h.file for h in results}
    assert "empty.py" not in files
    assert "normal.py" in files


# ---------------------------------------------------------------------------
# Decay math
# ---------------------------------------------------------------------------


def test_decay_90_day_half_life() -> None:
    """A commit exactly 90 days ago contributes half the weight of a commit at t=0."""
    ref = _REF
    file_commit_timestamps = {
        "decay_test.py": [ref, _ts_ago(90)],
    }
    file_loc = {"decay_test.py": 100}
    file_cyclomatic_total = {"decay_test.py": 10}

    results = compute_hotspots(
        file_commit_timestamps,
        file_loc,
        file_cyclomatic_total,
        reference_ts=ref,
        half_life_days=90.0,
    )

    assert len(results) == 1
    # weight at t=0 → 0.5^0 = 1.0
    # weight at 90 days → 0.5^1 = 0.5
    # total churn = 1.5
    assert results[0].churn == pytest.approx(1.5, rel=1e-6)


def test_decay_commit_at_reference_contributes_weight_one() -> None:
    """A commit at exactly reference_ts has age_days=0 → weight=1."""
    ref = _REF
    file_commit_timestamps = {"f.py": [ref]}
    file_loc = {"f.py": 50}
    file_cyclomatic_total = {"f.py": 10}

    results = compute_hotspots(
        file_commit_timestamps, file_loc, file_cyclomatic_total, reference_ts=ref
    )

    assert results[0].churn == pytest.approx(1.0)


def test_decay_commit_future_timestamp_clamps_to_zero_age() -> None:
    """age_days is clamped to 0 for commits with ts > reference_ts."""
    ref = _REF
    future_ts = ref + 1000  # slightly in the future
    file_commit_timestamps = {"f.py": [future_ts]}
    file_loc = {"f.py": 50}
    file_cyclomatic_total = {"f.py": 10}

    results = compute_hotspots(
        file_commit_timestamps,
        file_loc,
        file_cyclomatic_total,
        reference_ts=ref,
        # window_days must be large enough to include future_ts
        window_days=365,
    )

    # age = max(0, -1000/86400) = 0 → weight = 1.0
    assert results[0].churn == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Trend detection
# ---------------------------------------------------------------------------


def test_trend_accelerating_more_commits_in_newer_half() -> None:
    """More commits in the newer half of the window → trend='accelerating'."""
    ref = _REF
    # window 180 days: midpoint at 90 days ago
    # older half: 90..180 days ago; newer half: 0..90 days ago
    file_commit_timestamps = {
        "a.py": [
            _ts_ago(10),  # newer
            _ts_ago(20),  # newer
            _ts_ago(100),  # older
        ]
    }
    file_loc = {"a.py": 100}
    file_cyclomatic_total = {"a.py": 10}

    results = compute_hotspots(
        file_commit_timestamps, file_loc, file_cyclomatic_total, reference_ts=ref
    )

    assert results[0].trend == "accelerating"


def test_trend_cooling_more_commits_in_older_half() -> None:
    """More commits in the older half → trend='cooling'."""
    ref = _REF
    file_commit_timestamps = {
        "b.py": [
            _ts_ago(100),  # older
            _ts_ago(110),  # older
            _ts_ago(20),  # newer
        ]
    }
    file_loc = {"b.py": 100}
    file_cyclomatic_total = {"b.py": 10}

    results = compute_hotspots(
        file_commit_timestamps, file_loc, file_cyclomatic_total, reference_ts=ref
    )

    assert results[0].trend == "cooling"


def test_trend_stable_equal_counts_in_both_halves() -> None:
    """Equal commits in older and newer halves → trend='stable'."""
    ref = _REF
    file_commit_timestamps = {
        "c.py": [
            _ts_ago(10),  # newer
            _ts_ago(100),  # older
        ]
    }
    file_loc = {"c.py": 100}
    file_cyclomatic_total = {"c.py": 10}

    results = compute_hotspots(
        file_commit_timestamps, file_loc, file_cyclomatic_total, reference_ts=ref
    )

    assert results[0].trend == "stable"


# ---------------------------------------------------------------------------
# Determinism and sort order
# ---------------------------------------------------------------------------


def test_sort_is_score_desc_then_file_asc() -> None:
    """Output is sorted by (score desc, file asc) deterministically."""
    ref = _REF
    # Give all files exactly the same churn and loc so we can control rank
    # purely through cyclomatic.
    commits = [_ts_ago(5)]
    file_commit_timestamps = {
        "alpha.py": commits,
        "beta.py": commits,
        "gamma.py": commits,
    }
    file_loc = {"alpha.py": 100, "beta.py": 100, "gamma.py": 100}
    # different cyclomatic → different scores
    file_cyclomatic_total = {
        "alpha.py": 30,  # highest density
        "beta.py": 20,
        "gamma.py": 20,  # tied with beta; should sort by filename
    }

    results = compute_hotspots(
        file_commit_timestamps, file_loc, file_cyclomatic_total, reference_ts=ref
    )

    assert results[0].file == "alpha.py"
    # beta and gamma are tied on score; beta < gamma alphabetically
    assert results[1].file == "beta.py"
    assert results[2].file == "gamma.py"


def test_determinism_same_input_same_output() -> None:
    """Calling compute_hotspots twice with the same inputs produces identical output."""
    ref = _REF
    file_commit_timestamps = {
        "x.py": [_ts_ago(5), _ts_ago(30)],
        "y.py": [_ts_ago(10)],
    }
    file_loc = {"x.py": 100, "y.py": 80}
    file_cyclomatic_total = {"x.py": 20, "y.py": 15}

    r1 = compute_hotspots(file_commit_timestamps, file_loc, file_cyclomatic_total, reference_ts=ref)
    r2 = compute_hotspots(file_commit_timestamps, file_loc, file_cyclomatic_total, reference_ts=ref)

    assert [h.file for h in r1] == [h.file for h in r2]
    assert [h.score for h in r1] == [h.score for h in r2]


# ---------------------------------------------------------------------------
# Division-by-zero guards
# ---------------------------------------------------------------------------


def test_single_file_does_not_crash() -> None:
    """A single file is the only candidate — max_churn == churn → score 100."""
    ref = _REF
    results = compute_hotspots(
        {"only.py": [_ts_ago(1)]},
        {"only.py": 100},
        {"only.py": 10},
        reference_ts=ref,
    )

    assert len(results) == 1
    assert results[0].score == pytest.approx(100.0)


def test_empty_inputs_return_empty_list() -> None:
    """No files → empty result, no exception."""
    results = compute_hotspots({}, {}, {}, reference_ts=_REF)
    assert results == []


def test_all_files_same_churn_and_density_no_crash() -> None:
    """All files with identical churn and density → all score 100 (both maxima == actual)."""
    ref = _REF
    commits = [_ts_ago(5)]
    file_commit_timestamps = {"a.py": commits, "b.py": commits}
    file_loc = {"a.py": 100, "b.py": 100}
    file_cyclomatic_total = {"a.py": 10, "b.py": 10}

    results = compute_hotspots(
        file_commit_timestamps, file_loc, file_cyclomatic_total, reference_ts=ref
    )

    assert len(results) == 2
    for h in results:
        assert h.score == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Score formula spot-check
# ---------------------------------------------------------------------------


def test_score_formula() -> None:
    """Spot-check the score formula with manually computed values."""
    ref = _REF
    # File A: churn=1.0 (one commit at ref), density=0.5 (cyc=50, loc=100)
    # File B: churn=0.5^1=0.5 (one commit 90d ago), density=0.25 (cyc=25, loc=100)
    # max_churn=1.0, max_density=0.5
    # score(A) = (1.0/1.0) * (0.5/0.5) * 100 = 100
    # score(B) = (0.5/1.0) * (0.25/0.5) * 100 = 25
    file_commit_timestamps = {
        "a.py": [ref],
        "b.py": [_ts_ago(90)],
    }
    file_loc = {"a.py": 100, "b.py": 100}
    file_cyclomatic_total = {"a.py": 50, "b.py": 25}

    results = compute_hotspots(
        file_commit_timestamps, file_loc, file_cyclomatic_total, reference_ts=ref
    )

    by_file = {h.file: h for h in results}
    assert by_file["a.py"].score == pytest.approx(100.0, rel=1e-5)
    assert by_file["b.py"].score == pytest.approx(25.0, rel=1e-5)


# ---------------------------------------------------------------------------
# select_hotspots — worst-decile surface threshold
# ---------------------------------------------------------------------------


def _hs(file: str, score: float):
    from shared.types import Hotspot

    return Hotspot(
        file=file, churn=1.0, complexity_density=1.0, score=score, trend="stable"
    )


def test_select_hotspots_keeps_worst_decile() -> None:
    from agent.graph_analyzer.churn import select_hotspots

    ranked = [_hs(f"f{i:02d}.py", float(100 - i)) for i in range(20)]  # 20 scored files
    sel = select_hotspots(ranked)
    assert len(sel) == 2  # ceil(0.10 * 20)
    assert [h.file for h in sel] == ["f00.py", "f01.py"]  # the two highest scores


def test_select_hotspots_excludes_zero_score() -> None:
    from agent.graph_analyzer.churn import select_hotspots

    assert select_hotspots([_hs("a.py", 0.0)]) == []


def test_select_hotspots_rounds_up_small_sets() -> None:
    from agent.graph_analyzer.churn import select_hotspots

    # 3 scored files → ceil(0.10*3) = 1
    sel = select_hotspots([_hs("a.py", 9.0), _hs("b.py", 5.0), _hs("c.py", 1.0)])
    assert [h.file for h in sel] == ["a.py"]
