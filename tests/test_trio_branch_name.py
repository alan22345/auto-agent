"""Tests for trio branch naming — including the init / consult sub-branches.

Git stores refs on disk as files: ``refs/heads/foo`` and ``refs/heads/foo/bar``
cannot both exist, because the first is a regular file and the second
needs ``foo`` to be a directory. Production hit this with the integration
branch ``auto-agent/<slug>-<id>`` and an init head branch named
``<integration_branch>/init``: the architect's ``git checkout -B`` failed
with "cannot lock ref … exists; cannot create …", and the subsequent push
reported "src refspec … does not match any". Task 4 stuck in
ARCHITECT_BACKLOG_EMIT on 2026-05-15 was the production repro.

The fix is to derive a sibling name with a dash separator instead of a
slash. These tests assert the sibling shape both abstractly and by
attempting the actual ``git`` operation against a real repository.
"""

from __future__ import annotations

import subprocess

import pytest

from agent.lifecycle.trio.branch_name import (
    consult_branch_name,
    init_branch_name,
    integration_branch_name,
)


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo_with_integration_branch(tmp_path, integration_branch: str) -> None:
    """Stand up a git repo with one commit and ``integration_branch`` checked out."""
    assert _git(["init", "-q"], tmp_path).returncode == 0
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "a").write_text("a")
    _git(["add", "."], tmp_path)
    assert _git(["commit", "-qm", "a"], tmp_path).returncode == 0
    assert _git(["checkout", "-qb", integration_branch], tmp_path).returncode == 0


def test_init_branch_name_is_sibling_not_subpath():
    integration = integration_branch_name(4, "Parallel universe screen")
    init = init_branch_name(integration)
    assert not init.startswith(integration + "/"), (
        f"D/F conflict: {init!r} is a sub-path of {integration!r}"
    )
    assert init.startswith(integration)


def test_consult_branch_name_is_sibling_not_subpath():
    integration = integration_branch_name(4, "Parallel universe screen")
    consult = consult_branch_name(integration, ts=1234567890)
    assert not consult.startswith(integration + "/"), (
        f"D/F conflict: {consult!r} is a sub-path of {integration!r}"
    )
    assert consult.startswith(integration)
    assert "1234567890" in consult


@pytest.mark.parametrize(
    "integration_branch",
    [
        "auto-agent/parallel-universe-screen-4",  # new shape
        "trio/4",  # legacy shape
    ],
)
def test_init_head_branch_creatable_against_real_git(tmp_path, integration_branch):
    """``git checkout -B <init>`` must succeed when the integration branch
    already exists — guards against the refs D/F-conflict regression."""
    _init_repo_with_integration_branch(tmp_path, integration_branch)
    head = init_branch_name(integration_branch)
    res = _git(["checkout", "-B", head], tmp_path)
    assert res.returncode == 0, f"checkout failed: {res.stderr!r}"


@pytest.mark.parametrize(
    "integration_branch",
    [
        "auto-agent/parallel-universe-screen-4",
        "trio/4",
    ],
)
def test_consult_head_branch_creatable_against_real_git(tmp_path, integration_branch):
    _init_repo_with_integration_branch(tmp_path, integration_branch)
    head = consult_branch_name(integration_branch, ts=1234567890)
    res = _git(["checkout", "-B", head], tmp_path)
    assert res.returncode == 0, f"checkout failed: {res.stderr!r}"
