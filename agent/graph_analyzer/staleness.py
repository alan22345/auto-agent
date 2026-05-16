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
from dataclasses import dataclass


@dataclass(frozen=True)
class Staleness:
    """Comparison result between a stored graph's SHA and a workspace's HEAD.

    Attributes:
        graph_sha: The ``commit_sha`` recorded on the ``RepoGraph`` row
            (or in the blob) at analysis time.
        workspace_sha: Current ``HEAD`` of ``workspace_path``, or ``None``
            when it could not be determined.
        drifted: ``True`` whenever the two SHAs differ — including the
            "can't determine workspace SHA" case (treated as drifted by
            convention so the agent never silently trusts a graph it
            cannot verify against).
    """

    graph_sha: str
    workspace_sha: str | None
    drifted: bool


def compute_staleness(*, graph_sha: str, workspace_path: str) -> Staleness:
    """Compare ``graph_sha`` against the current HEAD of ``workspace_path``.

    Pure-ish helper: the only side effect is a ``git rev-parse HEAD``
    subprocess invocation against ``workspace_path``. The function never
    raises; every error path resolves to ``Staleness(graph_sha,
    workspace_sha=None, drifted=True)``.

    Args:
        graph_sha: SHA recorded on the ``RepoGraph`` row.
        workspace_path: Absolute path to a (presumably git-tracked)
            workspace directory. Empty string or missing directory →
            ``workspace_sha=None``.

    Returns:
        A :class:`Staleness` instance describing the comparison.
    """
    workspace_sha = _read_head_sha(workspace_path)
    if workspace_sha is None:
        return Staleness(
            graph_sha=graph_sha,
            workspace_sha=None,
            drifted=True,
        )
    return Staleness(
        graph_sha=graph_sha,
        workspace_sha=workspace_sha,
        drifted=workspace_sha != graph_sha,
    )


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
