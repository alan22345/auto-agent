"""Staleness primitive for the code-graph (ADR-016 Phase 6).

The ``query_repo_graph`` agent tool surfaces a ``staleness`` envelope on
every response so the agent can decide whether to trust a stored
analysis. This module owns the comparison between a graph's recorded
``commit_sha`` and the current ``HEAD`` of an on-disk workspace.

Conservative behaviour: any failure to inspect the workspace (missing
directory, not a git checkout, ``git`` invocation fails, permission
denied) is treated as *drifted* with ``workspace_sha=None``. The agent
sees an honest "we don't know" rather than a false-positive "fresh"
signal.

The module is pure with respect to its inputs apart from the one
``subprocess.run`` call against ``git``; it never raises — every error
path returns a :class:`Staleness` value.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

# How long one ls-remote answer is reused. Graph queries arrive in
# bursts within a task; one network round-trip per minute per repo is
# plenty, and a stale-by-a-minute origin SHA is still infinitely more
# honest than never asking origin at all.
ORIGIN_CACHE_TTL_SECONDS = 60.0

_origin_cache: dict[tuple[str, str], tuple[float, str | None]] = {}


def clear_origin_cache() -> None:
    """Reset the ls-remote cache (used by tests)."""
    _origin_cache.clear()


@dataclass(frozen=True)
class Staleness:
    """Comparison result between a stored graph's SHA and reality.

    Attributes:
        graph_sha: The ``commit_sha`` recorded on the ``RepoGraph`` row
            (or in the blob) at analysis time.
        workspace_sha: Current ``HEAD`` of ``workspace_path``, or ``None``
            when it could not be determined.
        drifted: ``True`` when the graph is behind ``origin_sha`` (the
            authoritative signal, ADR-024); falls back to the workspace
            comparison when origin can't be asked. The "can't determine
            anything" case is drifted by convention so the agent never
            silently trusts a graph it cannot verify against.
        origin_sha: Tip of ``origin/<analysis_branch>`` per
            ``git ls-remote`` (TTL-cached), or ``None`` when no branch
            was supplied or origin couldn't be reached.
    """

    graph_sha: str
    workspace_sha: str | None
    drifted: bool
    origin_sha: str | None = None


def compute_staleness(
    *,
    graph_sha: str,
    workspace_path: str,
    analysis_branch: str | None = None,
) -> Staleness:
    """Compare ``graph_sha`` against reality; never raises.

    With ``analysis_branch``, reality is the tip of
    ``origin/<analysis_branch>`` (``git ls-remote`` from the workspace,
    TTL-cached) — the workspace HEAD only moves on refresh, so comparing
    against it alone reports "fresh" forever once an analysis lands.
    Without a branch, or when origin is unreachable, falls back to the
    workspace-HEAD comparison; if that can't be read either, the result
    is drifted with ``workspace_sha=None``.
    """
    workspace_sha = _read_head_sha(workspace_path)
    origin_sha = (
        _read_origin_sha(workspace_path, analysis_branch)
        if analysis_branch and workspace_sha is not None
        else None
    )

    if origin_sha is not None:
        drifted = graph_sha != origin_sha
    elif workspace_sha is not None:
        drifted = graph_sha != workspace_sha
    else:
        drifted = True

    return Staleness(
        graph_sha=graph_sha,
        workspace_sha=workspace_sha,
        drifted=drifted,
        origin_sha=origin_sha,
    )


def _read_origin_sha(workspace_path: str, branch: str) -> str | None:
    """TTL-cached tip of ``origin/<branch>``; None when origin can't say.

    Failures are cached too — a dead remote shouldn't be re-asked on
    every query in a burst.
    """
    key = (workspace_path, branch)
    now = time.monotonic()
    cached = _origin_cache.get(key)
    if cached is not None and now - cached[0] < ORIGIN_CACHE_TTL_SECONDS:
        return cached[1]
    sha = _ls_remote_branch_tip(workspace_path, branch)
    _origin_cache[key] = (now, sha)
    return sha


def _ls_remote_branch_tip(workspace_path: str, branch: str) -> str | None:
    """Ask origin for the tip of ``branch``; None on any failure."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first_line = result.stdout.strip().splitlines()[:1]
    if not first_line:
        return None
    sha = first_line[0].split("\t")[0].strip()
    return sha or None


def _read_head_sha(workspace_path: str) -> str | None:
    """Best-effort read of ``HEAD`` for ``workspace_path``.

    Returns ``None`` on any failure (missing path, not a git checkout,
    permission denied, ``git`` not installed, non-zero exit). All
    exceptions are swallowed by design — callers convert "couldn't
    read" into the drifted signal.
    """
    if not workspace_path:
        return None
    if not os.path.isdir(workspace_path):
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


__all__ = ["Staleness", "compute_staleness"]
