"""Phase 3 — cleanup-branch lifecycle (real-git integration tests)."""

from __future__ import annotations

import pytest

from agent.health_loop import cleanup_branch
from agent.health_loop.cleanup_branch import DEFAULT_CLEANUP_BRANCH


@pytest.mark.asyncio
async def test_force_push_refuses_branch_not_in_allowlist():
    """The guardrail: force-pushing anything outside the allowlist raises
    BEFORE any git command runs."""
    from unittest.mock import AsyncMock, patch

    sh_run = AsyncMock()
    with (
        patch.object(cleanup_branch.sh, "run", sh_run),
        pytest.raises(ValueError, match="allowlist"),
    ):
        await cleanup_branch._force_push_cleanup(
            workspace="/tmp/x",
            cleanup_branch="main",  # NOT the cleanup branch
            allowed_branches={DEFAULT_CLEANUP_BRANCH},
        )
    sh_run.assert_not_called()  # never reached the push
