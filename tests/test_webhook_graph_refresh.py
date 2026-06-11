"""GitHub webhook → debounced graph refresh triggers (ADR-024).

Any event that moves a branch on origin — a push, or a PR merging —
schedules a debounced ``repo.graph_requested`` via
``orchestrator.graph_freshness``. The branch filter and the
graph-enabled check live in graph_freshness; the webhook handlers just
map the payload to ``(repo_id, branch)`` and fire-and-forget.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.webhooks import github as gh
from shared.models import Repo


class _SessionStub:
    """One-execute session yielding a fixed scalar (the repo lookup)."""

    def __init__(self, scalar):
        self._scalar = scalar

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def execute(self, _stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._scalar
        return result


def _repo(repo_id: int = 5) -> Repo:
    repo = MagicMock(spec=Repo)
    repo.id = repo_id
    repo.name = "demo"
    return repo


def _patch_repo_lookup(repo):
    factory = lambda: _SessionStub(repo)  # noqa: E731
    return patch.object(gh, "async_session", factory)


def _patch_refresh():
    return patch.object(gh, "request_graph_refresh_soon")


# ---------------------------------------------------------------------------
# push events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_to_branch_schedules_refresh() -> None:
    payload = {
        "ref": "refs/heads/main",
        "deleted": False,
        "repository": {"full_name": "owner/demo"},
    }
    with _patch_repo_lookup(_repo(5)), _patch_refresh() as refresh:
        await gh._handle_push(payload)

    refresh.assert_called_once_with(5, branch="main")


@pytest.mark.asyncio
async def test_push_branch_deletion_is_ignored() -> None:
    payload = {
        "ref": "refs/heads/main",
        "deleted": True,
        "repository": {"full_name": "owner/demo"},
    }
    with _patch_repo_lookup(_repo(5)), _patch_refresh() as refresh:
        await gh._handle_push(payload)

    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_push_to_unknown_repo_is_ignored() -> None:
    payload = {
        "ref": "refs/heads/main",
        "deleted": False,
        "repository": {"full_name": "owner/unknown"},
    }
    with _patch_repo_lookup(None), _patch_refresh() as refresh:
        await gh._handle_push(payload)

    refresh.assert_not_called()


# ---------------------------------------------------------------------------
# pull_request merged events
# ---------------------------------------------------------------------------


def _merged_pr_payload(*, merged: bool, head: str = "feature/human-work") -> dict:
    return {
        "action": "closed",
        "repository": {"full_name": "owner/demo"},
        "pull_request": {
            "html_url": "https://github.com/owner/demo/pull/9",
            "merged": merged,
            "head": {"ref": head},
            "base": {"ref": "main"},
        },
    }


@pytest.mark.asyncio
async def test_merged_pr_schedules_refresh_on_the_base_branch() -> None:
    """Any merged PR moves its base branch — including human PRs whose
    head doesn't start with auto-agent/ (no task transition happens)."""
    with _patch_repo_lookup(_repo(5)), _patch_refresh() as refresh:
        await gh._handle_pull_request(_merged_pr_payload(merged=True))

    refresh.assert_called_once_with(5, branch="main")


@pytest.mark.asyncio
async def test_closed_unmerged_pr_does_not_schedule_refresh() -> None:
    with _patch_repo_lookup(_repo(5)), _patch_refresh() as refresh:
        await gh._handle_pull_request(_merged_pr_payload(merged=False))

    refresh.assert_not_called()
