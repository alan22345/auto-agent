"""Tests for agent.po_analyzer._brief_is_fresh."""

from datetime import UTC, datetime, timedelta

from agent.po_analyzer import _brief_is_fresh


def test_none_brief_is_not_fresh():
    now = datetime(2026, 5, 12, tzinfo=UTC)
    assert _brief_is_fresh(None, now, max_age_days=7) is False


def test_brief_within_max_age_is_fresh():
    now = datetime(2026, 5, 12, tzinfo=UTC)
    created_at = now - timedelta(days=3)
    assert _brief_is_fresh_for_test(created_at, now, 7) is True


def test_brief_at_exact_age_is_not_fresh():
    now = datetime(2026, 5, 12, tzinfo=UTC)
    created_at = now - timedelta(days=7)
    assert _brief_is_fresh_for_test(created_at, now, 7) is False


def test_brief_older_than_max_age_is_not_fresh():
    now = datetime(2026, 5, 12, tzinfo=UTC)
    created_at = now - timedelta(days=8)
    assert _brief_is_fresh_for_test(created_at, now, 7) is False


def _brief_is_fresh_for_test(created_at, now, max_age_days):
    """Build a minimal duck-typed brief; the real function only reads .created_at."""
    class FakeBrief:
        pass
    b = FakeBrief()
    b.created_at = created_at
    return _brief_is_fresh(b, now, max_age_days)
