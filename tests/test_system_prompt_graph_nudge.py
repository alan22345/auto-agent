"""Tests for the code-graph system-prompt nudge (ADR-016 Phase 6 §12).

When a task's repo has a ``RepoGraphConfig`` row AND a completed
analysis (``last_analysis_id`` non-null), the system prompt gains a
paragraph nudging the agent to call ``query_repo_graph`` before
grepping. The paragraph is suppressed for repos without the config (the
opt-in is missing) or with the config but no completed analysis yet
(the tool would return errors).
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used as runtime annotation by pytest fixtures
from unittest.mock import MagicMock, patch

import pytest

from agent.context.system import SystemPromptBuilder

_NUDGE_MARKER = "This repo has a code graph available"


class _ConfigSession:
    """Async-session stub returning a fixed ``RepoGraphConfig``-like object."""

    def __init__(self, config: object | None) -> None:
        self._config = config

    async def __aenter__(self) -> _ConfigSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, _stmt: object) -> object:
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._config
        return result


def _patch_session(config: object | None):
    """Patch ``agent.context.system.async_session`` to return ``config``."""
    factory = lambda: _ConfigSession(config)  # noqa: E731
    return patch("agent.context.system.async_session", new=factory)


def _bare_workspace(tmp_path: Path) -> str:
    """Empty workspace — no CLAUDE.md, no .git, no repo_map to clutter."""
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


@pytest.mark.asyncio
async def test_nudge_appears_when_graph_enabled_and_analysis_present(
    tmp_path: Path,
) -> None:
    ws = _bare_workspace(tmp_path)
    cfg = MagicMock()
    cfg.last_analysis_id = 42

    builder = SystemPromptBuilder()
    with _patch_session(cfg):
        prompt = await builder.build(ws, repo_id=7)

    assert _NUDGE_MARKER in prompt
    # The nudge documents the tool's op list — sanity check a couple of
    # ops to ensure the agent gets the full surface.
    assert "callers_of" in prompt
    assert "violates_boundaries" in prompt
    assert "staleness" in prompt


@pytest.mark.asyncio
async def test_nudge_absent_when_no_config_row_exists(
    tmp_path: Path,
) -> None:
    ws = _bare_workspace(tmp_path)
    builder = SystemPromptBuilder()
    with _patch_session(None):
        prompt = await builder.build(ws, repo_id=7)

    assert _NUDGE_MARKER not in prompt


@pytest.mark.asyncio
async def test_nudge_absent_when_config_exists_but_no_analysis_yet(
    tmp_path: Path,
) -> None:
    ws = _bare_workspace(tmp_path)
    cfg = MagicMock()
    cfg.last_analysis_id = None  # Opted in, but analyser hasn't run yet.

    builder = SystemPromptBuilder()
    with _patch_session(cfg):
        prompt = await builder.build(ws, repo_id=7)

    assert _NUDGE_MARKER not in prompt


@pytest.mark.asyncio
async def test_nudge_absent_when_repo_id_not_provided(tmp_path: Path) -> None:
    """Existing call sites that don't pass ``repo_id`` continue to
    produce a prompt without the nudge — additive integration only."""
    ws = _bare_workspace(tmp_path)
    builder = SystemPromptBuilder()

    # No patching needed — the builder shouldn't even consult the DB
    # without a repo_id.
    prompt = await builder.build(ws)
    assert _NUDGE_MARKER not in prompt


@pytest.mark.asyncio
async def test_nudge_resilient_to_db_failure(tmp_path: Path) -> None:
    """A DB exception during the lookup must not break system-prompt
    construction — we log and skip the nudge."""
    ws = _bare_workspace(tmp_path)
    builder = SystemPromptBuilder()

    class _BoomSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, _stmt):
            raise RuntimeError("db is down")

    with patch(
        "agent.context.system.async_session",
        new=lambda: _BoomSession(),
    ):
        prompt = await builder.build(ws, repo_id=7)

    assert _NUDGE_MARKER not in prompt
