"""ADR-015 §6 — origin-based standin selection.

In freeform, the standin at every gate is determined by the task's
origin:

  * task came from a PO suggestion → :class:`POStandin`.
  * task came from an improvement-agent suggestion →
    :class:`ImprovementAgentStandin`.
  * user-created task (manual / slack / telegram / linear) →
    :class:`POStandin` (default per the ADR).

The selector reads ``task.source`` and, when the task was spawned from a
``Suggestion`` (``source_id == "suggestion:<id>"``), peeks at the
suggestion's ``category`` to disambiguate PO vs improvement.

These tests pin selection behaviour using in-memory stubs so they don't
require a Postgres test database.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.lifecycle.standin import (
    ImprovementAgentStandin,
    POStandin,
    select_standin,
)


def _task(
    *,
    source: str = "manual",
    source_id: str = "",
    repo_id: int = 1,
    task_id: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        source=source,
        source_id=source_id,
        repo_id=repo_id,
        mode_override=None,
    )


def _repo(mode: str = "freeform") -> SimpleNamespace:
    return SimpleNamespace(id=1, mode=mode, name="acme/widget")


class _FakeSuggestionsRepo:
    """Tiny in-memory shim for the suggestion lookup the selector needs.

    The real selector consults the DB through a small dependency
    function (see ``agent/lifecycle/standin.py``); tests inject this
    shim to keep them DB-free.
    """

    def __init__(self, by_id: dict[int, str]) -> None:
        self._by_id = by_id

    async def get_category(self, suggestion_id: int) -> str | None:
        return self._by_id.get(suggestion_id)


@pytest.mark.asyncio
async def test_po_suggestion_selects_po_standin() -> None:
    task = _task(source="freeform", source_id="suggestion:7")
    repo = _repo()
    suggestions = _FakeSuggestionsRepo({7: "ux_gap"})
    standin = await select_standin(task, repo, gate="grill", suggestion_repo=suggestions)
    assert isinstance(standin, POStandin)


@pytest.mark.asyncio
async def test_improvement_suggestion_selects_improvement_agent_standin() -> None:
    task = _task(source="freeform", source_id="suggestion:42")
    repo = _repo()
    suggestions = _FakeSuggestionsRepo({42: "architecture"})
    standin = await select_standin(task, repo, gate="plan_approval", suggestion_repo=suggestions)
    assert isinstance(standin, ImprovementAgentStandin)


@pytest.mark.asyncio
async def test_user_created_task_defaults_to_po_standin() -> None:
    """No source_id pointing at a suggestion → default to PO per §6."""

    task = _task(source="manual", source_id="")
    repo = _repo()
    suggestions = _FakeSuggestionsRepo({})
    standin = await select_standin(task, repo, gate="design_approval", suggestion_repo=suggestions)
    assert isinstance(standin, POStandin)


@pytest.mark.asyncio
async def test_slack_user_task_defaults_to_po_standin() -> None:
    task = _task(source="slack", source_id="C123:1234.5678")
    repo = _repo()
    suggestions = _FakeSuggestionsRepo({})
    standin = await select_standin(task, repo, gate="pr_review", suggestion_repo=suggestions)
    assert isinstance(standin, POStandin)


@pytest.mark.asyncio
async def test_unknown_suggestion_id_defaults_to_po() -> None:
    """If the source_id parses but no row exists (e.g. deleted), default
    to PO — never escape to the user; that defeats freeform."""

    task = _task(source="freeform", source_id="suggestion:999")
    repo = _repo()
    suggestions = _FakeSuggestionsRepo({})  # 999 not present
    standin = await select_standin(task, repo, gate="grill", suggestion_repo=suggestions)
    assert isinstance(standin, POStandin)


@pytest.mark.asyncio
async def test_malformed_source_id_defaults_to_po() -> None:
    """``source_id`` strings that don't follow ``suggestion:<int>`` shape
    (e.g. Slack ``ts`` strings) fall through to the PO default."""

    task = _task(source="manual", source_id="suggestion:not_a_number")
    repo = _repo()
    suggestions = _FakeSuggestionsRepo({})
    standin = await select_standin(task, repo, gate="grill", suggestion_repo=suggestions)
    assert isinstance(standin, POStandin)
