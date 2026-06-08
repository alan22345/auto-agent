"""Churn x complexity hotspot ranking (ADR-016 quality layer §5, Phase 12).

Design
------
The module is split into a **pure algorithm** layer and an **I/O layer**:

* :func:`compute_hotspots` is deterministic and dependency-free — it only
  does arithmetic on the dicts passed in. It can be unit-tested without git,
  a filesystem, or any async runtime.
* :func:`collect_git_churn` is the only I/O entry point. It shells out to git
  via :mod:`subprocess` and returns ``(None, {})`` on any failure so the
  pipeline remains safe against non-git workspaces (e.g. every existing
  pipeline test that runs on a plain ``tmp_path`` copy of a static fixture).
* :func:`count_loc` reads a file on disk and counts its lines. Returns 0 if
  the file is unreadable. Used by the pipeline to build ``file_loc``.

Decay model
-----------
churn(file) = Σ 0.5 ** (age_days / half_life_days)
where age_days = max(0, (reference_ts - commit_ts) / 86400).
The default half-life is 90 days, meaning a commit 90 days before the
reference time contributes half the weight of a commit at the reference time.

Scoring
-------
complexity_density(file) = cyclomatic_total / loc  (0 if loc is 0)
score = (churn / max_churn) * (density / max_density) * 100

A file scores > 0 only if it has BOTH churn > 0 AND density > 0.
Only files that (a) appear in ``file_cyclomatic_total`` with loc > 0 AND
(b) have at least one commit in the window are included in the output.
No surface threshold is applied here — consumers filter as needed.

Trend
-----
The window [reference_ts - window_days, reference_ts] is split at its
midpoint. Commits in the newer half vs the older half are counted:
  newer > older → "accelerating"
  newer < older → "cooling"
  equal          → "stable"

Reference time
--------------
reference_ts is the HEAD committer timestamp, making results deterministic
from the repo state. Non-git workspaces yield ``ref_ts=None`` from
``collect_git_churn``; callers then set ``blob.hotspots = []``.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from typing import Literal

from shared.types import Hotspot

# ---------------------------------------------------------------------------
# Pure algorithm
# ---------------------------------------------------------------------------


def compute_hotspots(
    file_commit_timestamps: dict[str, list[int]],
    file_loc: dict[str, int],
    file_cyclomatic_total: dict[str, int],
    *,
    reference_ts: int,
    half_life_days: float = 90.0,
    window_days: int = 180,
) -> list[Hotspot]:
    """Rank files by churn x complexity score.

    Parameters
    ----------
    file_commit_timestamps:
        Mapping from workspace-relative file path to a list of committer Unix
        timestamps for commits that touched the file within the window.
    file_loc:
        Mapping from file path to lines of code.
    file_cyclomatic_total:
        Mapping from file path to the sum of cyclomatic complexity over all
        function-kind nodes in the file.
    reference_ts:
        Unix timestamp used as "now" for decay and trend calculations. Should
        be the HEAD committer timestamp so results are deterministic from repo
        state.
    half_life_days:
        Decay half-life in days (default 90). A commit ``half_life_days``
        before ``reference_ts`` contributes half the weight of a commit at
        ``reference_ts``.
    window_days:
        Total window length in days (default 180). Only commits within this
        window are considered.

    Returns
    -------
    list[Hotspot]
        Sorted deterministically by (score descending, file ascending).
        No surface threshold is applied — consumers filter as needed.
    """
    window_start_ts = reference_ts - window_days * 86400
    midpoint_ts = reference_ts - (window_days / 2) * 86400

    # Candidate set: files with a defined density (cyclomatic_total + loc > 0)
    # AND at least one commit in the window.
    candidate_files: set[str] = set()
    for f in file_cyclomatic_total:
        loc = file_loc.get(f, 0)
        if loc <= 0:
            continue
        commits = file_commit_timestamps.get(f, [])
        in_window = [ts for ts in commits if ts >= window_start_ts]
        if not in_window:
            continue
        candidate_files.add(f)

    if not candidate_files:
        return []

    # Compute per-file churn and density.
    churn_by_file: dict[str, float] = {}
    density_by_file: dict[str, float] = {}

    for f in candidate_files:
        loc = file_loc[f]
        cyclomatic = file_cyclomatic_total[f]
        density_by_file[f] = cyclomatic / loc  # loc > 0 guaranteed above

        commits = [ts for ts in file_commit_timestamps.get(f, []) if ts >= window_start_ts]
        churn = 0.0
        for ts in commits:
            age_days = max(0.0, (reference_ts - ts) / 86400.0)
            churn += 0.5 ** (age_days / half_life_days)
        churn_by_file[f] = churn

    max_churn = max(churn_by_file.values())
    max_density = max(density_by_file.values())

    hotspots: list[Hotspot] = []
    for f in candidate_files:
        churn = churn_by_file[f]
        density = density_by_file[f]
        # Guard divide-by-zero (all files have equal churn/density == 0)
        if max_churn <= 0.0 or max_density <= 0.0:
            score = 0.0
        else:
            score = (churn / max_churn) * (density / max_density) * 100.0

        # Trend: compare commit counts in older vs newer half of the window.
        all_in_window = [ts for ts in file_commit_timestamps.get(f, []) if ts >= window_start_ts]
        older_count = sum(1 for ts in all_in_window if ts < midpoint_ts)
        newer_count = sum(1 for ts in all_in_window if ts >= midpoint_ts)
        if newer_count > older_count:
            trend: Literal["accelerating", "stable", "cooling"] = "accelerating"
        elif newer_count < older_count:
            trend = "cooling"
        else:
            trend = "stable"

        hotspots.append(
            Hotspot(
                file=f,
                churn=churn,
                complexity_density=density,
                score=score,
                trend=trend,
            )
        )

    # Deterministic sort: score descending, file ascending as tiebreak.
    hotspots.sort(key=lambda h: (-h.score, h.file))
    return hotspots


def select_hotspots(
    hotspots: list[Hotspot],
    *,
    top_fraction: float = 0.10,
) -> list[Hotspot]:
    """Apply the surface threshold: a "hotspot" is among the worst
    ``top_fraction`` of files by score.

    ``compute_hotspots`` ranks *every* scored file (no threshold), which on a
    real repo flags almost all files. This keeps only the top
    ``ceil(top_fraction * scored_count)`` so "hotspot" stays discriminating.
    Files scoring 0 are never flagged. The result is score-descending.
    """
    import math

    scored = sorted(
        (h for h in hotspots if h.score > 0.0),
        key=lambda h: (-h.score, h.file),
    )
    keep = math.ceil(top_fraction * len(scored)) if scored else 0
    return scored[:keep]


# ---------------------------------------------------------------------------
# LOC helper
# ---------------------------------------------------------------------------


def count_loc(workspace: str, file: str) -> int:
    """Return the line count of *workspace/file* on disk.

    Returns 0 if the file is missing or unreadable for any reason.
    """
    import os

    path = os.path.join(workspace, file)
    try:
        with open(path, "rb") as fh:
            content = fh.read()
        # Count newlines; add 1 if the file is non-empty and doesn't end
        # with a newline so that single-line files without a trailing
        # newline still count as 1.
        if not content:
            return 0
        lines = content.count(b"\n")
        if not content.endswith(b"\n"):
            lines += 1
        return lines
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Git I/O helper
# ---------------------------------------------------------------------------


def collect_git_churn(
    workspace: str,
    window_days: int = 180,
) -> tuple[int | None, dict[str, list[int]]]:
    """Return ``(reference_ts, {file: [commit_ts, ...]})`` for the workspace.

    ``reference_ts`` is the HEAD committer timestamp. ``file`` keys are
    workspace-relative paths matching the ``file`` field on ``Node`` values.

    If the workspace is NOT a git repository, or git fails for ANY reason,
    returns ``(None, {})`` without raising. Callers then produce zero
    hotspots (``blob.hotspots = []``).

    This function uses synchronous :mod:`subprocess` because the graph
    pipeline's hotspot wiring is called from a synchronous context at blob
    assembly time. All git failures are caught and silently return the empty
    result.
    """
    try:
        return _collect_git_churn_impl(workspace, window_days)
    except Exception:
        return (None, {})


def _collect_git_churn_impl(
    workspace: str,
    window_days: int,
) -> tuple[int | None, dict[str, list[int]]]:
    """Implementation — may raise; callers must wrap in try/except."""
    # --- HEAD committer timestamp -------------------------------------------
    head_result = subprocess.run(
        ["git", "-C", workspace, "log", "-1", "--pretty=format:%ct"],
        capture_output=True,
        text=True,
    )
    if head_result.returncode != 0 or not head_result.stdout.strip():
        return (None, {})

    reference_ts = int(head_result.stdout.strip())

    # --- Commits within the window ------------------------------------------
    # ``--no-merges`` keeps the signal clean; merge commits inflate churn
    # without representing real file changes.
    # Format: each commit outputs ``<hash>|<committer_ts>`` on one line,
    # followed by the list of changed files (one per line), followed by a
    # blank line separator.
    log_result = subprocess.run(
        [
            "git",
            "-C",
            workspace,
            "log",
            f"--since={window_days} days ago",
            "--no-merges",
            "--pretty=format:%H|%ct",
            "--name-only",
        ],
        capture_output=True,
        text=True,
    )
    if log_result.returncode != 0:
        return (None, {})

    file_commits: dict[str, list[int]] = defaultdict(list)
    current_ts: int | None = None

    for raw_line in log_result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            # Blank separator between commits — reset current timestamp.
            current_ts = None
            continue
        if "|" in line and len(line.split("|")) == 2:
            # Header line: ``<hash>|<ts>``
            _, ts_str = line.split("|", 1)
            try:
                current_ts = int(ts_str)
            except ValueError:
                current_ts = None
        else:
            # File path line.
            if current_ts is not None and line:
                # Normalise to forward slashes (git always outputs them but
                # be defensive on Windows).
                rel = line.replace("\\", "/")
                file_commits[rel].append(current_ts)

    return (reference_ts, dict(file_commits))
