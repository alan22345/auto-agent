"""Tests for the LGTM-driven auto-merge path.

When a PR review with state=approved arrives on a freeform-mode task,
auto-agent attempts to merge the PR. The gate checks:

- task.freeform_mode must be True (non-freeform falls through to manual review)
- mergeable_state from GitHub must be 'clean' or 'has_hooks' (CI green)
- 'dirty' triggers the conflict resolver instead of merging
- 'unstable'/other states fall through to AWAITING_REVIEW

The webhook itself only emits the event; the handler does the gating.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

import run
from run import (
    MERGE_OUTCOME_CI_BLOCKED,
    MERGE_OUTCOME_CONFLICT_DISPATCHED,
    MERGE_OUTCOME_FAILED,
    MERGE_OUTCOME_MERGED,
    _auto_merge_pr,
)
from shared.models import TaskStatus


@dataclass
class StubTask:
    id: int = 7
    pr_url: str = "https://github.com/owner/repo/pull/42"
    freeform_mode: bool = True
    status: TaskStatus = TaskStatus.AWAITING_REVIEW


# ---------------------------------------------------------------------------
# _auto_merge_pr — pre-emptive mergeable check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_merge_clean_state_calls_merge_api(monkeypatch):
    """mergeable_state=clean → merge API called → MERGED returned."""
    monkeypatch.setattr(run.settings, "github_token", "fake-token", raising=False)

    async def fake_state(_url):
        return {"mergeable_state": "clean"}

    class FakeResp:
        status_code = 200
        text = "{}"

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def put(self, *a, **k): return FakeResp()

    with patch("run._fetch_pr_state", side_effect=fake_state), \
         patch("run.httpx.AsyncClient", FakeClient):
        outcome = await _auto_merge_pr(StubTask())
    assert outcome == MERGE_OUTCOME_MERGED


@pytest.mark.asyncio
async def test_auto_merge_dirty_dispatches_conflict_resolver(monkeypatch):
    """mergeable_state=dirty → CONFLICT_DISPATCHED, conflict event emitted."""
    monkeypatch.setattr(run.settings, "github_token", "fake-token", raising=False)

    async def fake_state(_url):
        return {"mergeable_state": "dirty"}

    dispatched: list[tuple[int, str]] = []

    async def fake_dispatch(task, trigger):
        dispatched.append((task.id, trigger))

    async def fake_attempted(_id):
        return False

    with patch("run._fetch_pr_state", side_effect=fake_state), \
         patch("run._dispatch_conflict_resolution", side_effect=fake_dispatch), \
         patch("run._conflict_resolution_attempted", side_effect=fake_attempted):
        outcome = await _auto_merge_pr(StubTask())
    assert outcome == MERGE_OUTCOME_CONFLICT_DISPATCHED
    assert dispatched == [(7, "auto_merge")]


@pytest.mark.asyncio
async def test_auto_merge_dirty_after_one_attempt_returns_failed(monkeypatch):
    """If conflict resolution was already attempted, don't dispatch again."""
    monkeypatch.setattr(run.settings, "github_token", "fake-token", raising=False)

    async def fake_state(_url):
        return {"mergeable_state": "dirty"}

    async def fake_attempted(_id):
        return True

    dispatch_calls = []

    async def fake_dispatch(task, trigger):
        dispatch_calls.append(task.id)

    with patch("run._fetch_pr_state", side_effect=fake_state), \
         patch("run._conflict_resolution_attempted", side_effect=fake_attempted), \
         patch("run._dispatch_conflict_resolution", side_effect=fake_dispatch):
        outcome = await _auto_merge_pr(StubTask())
    assert outcome == MERGE_OUTCOME_FAILED
    assert dispatch_calls == []


@pytest.mark.asyncio
async def test_auto_merge_unstable_returns_ci_blocked(monkeypatch):
    """mergeable_state=unstable → CI_BLOCKED, no merge call."""
    monkeypatch.setattr(run.settings, "github_token", "fake-token", raising=False)

    async def fake_state(_url):
        return {"mergeable_state": "unstable"}

    with patch("run._fetch_pr_state", side_effect=fake_state):
        outcome = await _auto_merge_pr(StubTask())
    assert outcome == MERGE_OUTCOME_CI_BLOCKED


@pytest.mark.asyncio
async def test_auto_merge_no_pr_url_fails():
    """Without a PR URL, auto-merge returns FAILED immediately."""
    outcome = await _auto_merge_pr(StubTask(pr_url=""))
    assert outcome == MERGE_OUTCOME_FAILED


# ---------------------------------------------------------------------------
# _attempt_lgtm_merge — gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lgtm_on_non_freeform_task_skips_merge():
    """Non-freeform tasks must not auto-merge on LGTM."""
    task = StubTask(freeform_mode=False)

    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def commit(self): pass

    async def fake_get_task(_session, _id):
        return task

    merge_calls = []

    async def fake_merge(_t):
        merge_calls.append(1)
        return MERGE_OUTCOME_MERGED

    with patch("run.async_session", FakeSession), \
         patch("run.get_task", side_effect=fake_get_task), \
         patch("run._auto_merge_pr", side_effect=fake_merge):
        await run._attempt_lgtm_merge(task.id, trigger="approved by alice")
    assert merge_calls == []


@pytest.mark.asyncio
async def test_lgtm_freeform_clean_merges_and_transitions_done():
    """Freeform task + clean PR + already in AWAITING_REVIEW → straight to DONE."""
    task = StubTask(freeform_mode=True, status=TaskStatus.AWAITING_REVIEW)

    class FakeSession:
        def __init__(self):
            self.committed = False
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def commit(self): self.committed = True

    async def fake_get_task(_session, _id):
        return task

    transitions: list[tuple] = []

    async def fake_transition(_session, t, status, msg):
        transitions.append((status, msg))
        return t

    async def fake_merge(_t):
        return MERGE_OUTCOME_MERGED

    async def fake_try_start_queued(_session):
        pass

    with patch("run.async_session", FakeSession), \
         patch("run.get_task", side_effect=fake_get_task), \
         patch("run._auto_merge_pr", side_effect=fake_merge), \
         patch("run.transition", side_effect=fake_transition), \
         patch("run._try_start_queued", side_effect=fake_try_start_queued):
        await run._attempt_lgtm_merge(task.id, trigger="approved by alice")
    # Task was already in AWAITING_REVIEW → only one transition needed (DONE)
    assert len(transitions) == 1
    assert transitions[0][0].value == "done"


@pytest.mark.asyncio
async def test_lgtm_conflict_dispatched_does_not_transition():
    """When merge dispatched the resolver, the task stays in flight (no transition)."""
    task = StubTask(freeform_mode=True)

    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def commit(self): pass

    async def fake_get_task(_session, _id):
        return task

    transitions: list = []

    async def fake_transition(_session, t, status, msg):
        transitions.append(status)
        return t

    async def fake_merge(_t):
        return MERGE_OUTCOME_CONFLICT_DISPATCHED

    with patch("run.async_session", FakeSession), \
         patch("run.get_task", side_effect=fake_get_task), \
         patch("run._auto_merge_pr", side_effect=fake_merge), \
         patch("run.transition", side_effect=fake_transition):
        await run._attempt_lgtm_merge(task.id, trigger="approved by alice")
    assert transitions == []


@pytest.mark.asyncio
async def test_lgtm_ci_blocked_falls_through_to_review():
    """CI_BLOCKED outcome from AWAITING_CI → walks through to AWAITING_REVIEW."""
    task = StubTask(freeform_mode=True, status=TaskStatus.AWAITING_CI)

    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def commit(self): pass

    async def fake_get_task(_session, _id):
        return task

    transitions: list = []

    async def fake_transition(_session, t, status, msg):
        transitions.append((status, msg))
        return t

    async def fake_merge(_t):
        return MERGE_OUTCOME_CI_BLOCKED

    with patch("run.async_session", FakeSession), \
         patch("run.get_task", side_effect=fake_get_task), \
         patch("run._auto_merge_pr", side_effect=fake_merge), \
         patch("run.transition", side_effect=fake_transition):
        await run._attempt_lgtm_merge(task.id, trigger="approved by alice")
    assert len(transitions) == 1
    assert transitions[0][0].value == "awaiting_review"
    assert "CI" in transitions[0][1]
