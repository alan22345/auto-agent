"""Tests for ``agent.graph_analyzer.staleness`` (ADR-016 Phase 6).

The staleness primitive compares the SHA captured in a stored ``RepoGraph``
row against the current HEAD of an on-disk workspace, producing a
:class:`Staleness` value the ``query_repo_graph`` tool surfaces to the
agent in every response envelope.

Conservative behaviour: when the workspace cannot be inspected (missing
directory, not a git checkout, ``git`` invocation fails) the helper sets
``workspace_sha=None`` and ``drifted=True`` rather than pretending the
graph is fresh.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path  # noqa: TC003 — used as runtime annotation by pytest fixtures

import pytest

from agent.graph_analyzer.staleness import (
    Staleness,
    clear_origin_cache,
    compute_staleness,
)


def _init_git_repo(path: Path) -> str:
    """Init a tiny git repo at ``path``, make one commit, return its SHA.

    Uses ``-c`` to inject user identity so the test doesn't depend on the
    runner's global ``~/.gitconfig``.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    (path / "README").write_text("hi\n")
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "add", "README"],
        cwd=str(path),
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=str(path),
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert sha
    return sha


class TestComputeStaleness:
    def test_matching_shas_means_not_drifted(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        sha = _init_git_repo(ws)

        result = compute_staleness(graph_sha=sha, workspace_path=str(ws))

        assert isinstance(result, Staleness)
        assert result.graph_sha == sha
        assert result.workspace_sha == sha
        assert result.drifted is False

    def test_differing_shas_means_drifted(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        sha = _init_git_repo(ws)
        # A SHA that is clearly not the workspace's.
        stale_sha = "0" * 40

        result = compute_staleness(
            graph_sha=stale_sha,
            workspace_path=str(ws),
        )

        assert result.graph_sha == stale_sha
        assert result.workspace_sha == sha
        assert result.drifted is True

    def test_missing_workspace_returns_none_workspace_sha_and_drifted(
        self,
        tmp_path: Path,
    ) -> None:
        # Path that doesn't exist on disk at all.
        missing = tmp_path / "does-not-exist"
        assert not missing.exists()

        result = compute_staleness(
            graph_sha="abc123",
            workspace_path=str(missing),
        )

        assert result.graph_sha == "abc123"
        assert result.workspace_sha is None
        assert result.drifted is True

    def test_non_git_directory_returns_none_workspace_sha_and_drifted(
        self,
        tmp_path: Path,
    ) -> None:
        # Directory exists but is not a git checkout.
        ws = tmp_path / "not-git"
        ws.mkdir()
        # Sanity — no .git inside.
        assert not (ws / ".git").exists()

        result = compute_staleness(
            graph_sha="abc123",
            workspace_path=str(ws),
        )

        assert result.graph_sha == "abc123"
        assert result.workspace_sha is None
        assert result.drifted is True

    def test_empty_workspace_path_string_is_treated_as_missing(
        self,
    ) -> None:
        # Callers that have no workspace_path configured (shouldn't happen
        # in practice — RepoGraphConfig.workspace_path is NOT NULL) should
        # still get a safe Staleness with drifted=True rather than an
        # exception.
        result = compute_staleness(graph_sha="abc123", workspace_path="")

        assert result.workspace_sha is None
        assert result.drifted is True

    def test_workspace_path_with_no_read_permission_is_missing(
        self,
        tmp_path: Path,
    ) -> None:
        # Skip when running as root — chmod 0 still readable for root.
        if os.geteuid() == 0:
            return
        ws = tmp_path / "noperm"
        ws.mkdir()
        try:
            ws.chmod(0o000)
            result = compute_staleness(
                graph_sha="abc",
                workspace_path=str(ws),
            )
            assert result.workspace_sha is None
            assert result.drifted is True
        finally:
            ws.chmod(0o755)


def _clone(origin: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(dest)],
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, filename: str) -> str:
    (repo / filename).write_text("more\n")
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "add", filename],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", filename],
        cwd=str(repo),
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branch_of(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestOriginComparison:
    """ADR-024: ``drifted`` must reflect origin, not just the local clone.

    The analyser workspace HEAD only moves on refresh, so comparing
    against it alone reports "fresh" forever once an analysis lands.
    When ``analysis_branch`` is supplied, compute_staleness asks origin
    (``git ls-remote``) and compares the graph SHA against that.
    """

    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        clear_origin_cache()
        yield
        clear_origin_cache()

    def test_origin_ahead_means_drifted_even_when_workspace_matches(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        graph_sha = _init_git_repo(origin)
        ws = tmp_path / "ws"
        _clone(origin, ws)
        new_sha = _commit(origin, "later.txt")

        result = compute_staleness(
            graph_sha=graph_sha,
            workspace_path=str(ws),
            analysis_branch=_branch_of(origin),
        )

        assert result.workspace_sha == graph_sha  # clone never refreshed
        assert result.origin_sha == new_sha
        assert result.drifted is True

    def test_origin_matching_graph_means_fresh(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        graph_sha = _init_git_repo(origin)
        ws = tmp_path / "ws"
        _clone(origin, ws)

        result = compute_staleness(
            graph_sha=graph_sha,
            workspace_path=str(ws),
            analysis_branch=_branch_of(origin),
        )

        assert result.origin_sha == graph_sha
        assert result.drifted is False

    def test_no_remote_falls_back_to_workspace_comparison(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        sha = _init_git_repo(ws)  # plain repo, no origin configured

        result = compute_staleness(
            graph_sha=sha,
            workspace_path=str(ws),
            analysis_branch="main",
        )

        assert result.origin_sha is None
        assert result.drifted is False  # legacy semantics preserved

    def test_origin_lookup_is_cached_within_ttl(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        graph_sha = _init_git_repo(origin)
        ws = tmp_path / "ws"
        _clone(origin, ws)
        branch = _branch_of(origin)

        first = compute_staleness(
            graph_sha=graph_sha, workspace_path=str(ws), analysis_branch=branch
        )
        assert first.drifted is False

        _commit(origin, "later.txt")  # origin moves...
        cached = compute_staleness(
            graph_sha=graph_sha, workspace_path=str(ws), analysis_branch=branch
        )
        assert cached.drifted is False  # ...but the cached answer still serves

        clear_origin_cache()
        fresh = compute_staleness(
            graph_sha=graph_sha, workspace_path=str(ws), analysis_branch=branch
        )
        assert fresh.drifted is True

    def test_without_analysis_branch_behaviour_is_unchanged(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        sha = _init_git_repo(ws)

        result = compute_staleness(graph_sha=sha, workspace_path=str(ws))

        assert result.origin_sha is None
        assert result.drifted is False
