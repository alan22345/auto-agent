"""Tests for session-ID generation policies in agent/lifecycle/_naming.py.

Two contracts:
  - ``_session_id(task_id, created_at)`` is **deterministic** across calls,
    so planning → coding → clarification → review-fix can resume the same
    Claude CLI session. Concurrency is prevented by handler-level guards
    (``_active_planning`` etc.).
  - ``_fresh_session_id(task_id, label)`` is **unique per invocation**, used
    for one-shot agents (independent reviewer) that are explicitly designed
    NOT to resume. Deterministic hashes collide on retry — the Claude CLI
    provider rejects re-used session IDs with "already in use", which was
    the root cause of an observed reviewer crash.
"""

from __future__ import annotations

import uuid

from agent.lifecycle._naming import _fresh_session_id, _session_id


def test_session_id_is_deterministic_per_task():
    """Same (task_id, created_at) → same UUID, every call."""
    a = _session_id(42, "2026-05-02T10:00:00+00:00")
    b = _session_id(42, "2026-05-02T10:00:00+00:00")
    assert a == b
    # Sanity: it parses as a UUID
    uuid.UUID(a)


def test_session_id_differs_per_task():
    a = _session_id(1, "2026-05-02T10:00:00+00:00")
    b = _session_id(2, "2026-05-02T10:00:00+00:00")
    assert a != b


def test_session_id_handles_none_created_at():
    """Tasks without a created_at don't crash session-ID generation."""
    a = _session_id(1, None)
    b = _session_id(1, None)
    assert a == b
    uuid.UUID(a)


def test_fresh_session_id_is_unique_per_call():
    """Two calls for the same (task_id, label) MUST produce different UUIDs.

    Regression: the independent reviewer used to hash deterministically on
    (task_id, created_at), so a retry collided with the still-registered
    first session and the Claude CLI rejected it.
    """
    a = _fresh_session_id(99, "review")
    b = _fresh_session_id(99, "review")
    assert a != b
    uuid.UUID(a)
    uuid.UUID(b)


def test_fresh_session_id_uniqueness_at_volume():
    """Even at high call volume the IDs don't collide (uuid4-backed)."""
    ids = {_fresh_session_id(7, "review") for _ in range(1000)}
    assert len(ids) == 1000


def test_fresh_session_id_label_does_not_collide_with_task_id():
    """Different labels for the same task produce different IDs (defence in
    depth — currently only 'review' uses _fresh_session_id, but if a future
    one-shot agent uses 'analysis' or 'audit', they shouldn't collide)."""
    ids = {_fresh_session_id(7, "review") for _ in range(50)}
    ids |= {_fresh_session_id(7, "audit") for _ in range(50)}
    assert len(ids) == 100
