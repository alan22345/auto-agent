"""Picker reply-parsing tests. Pure function tests, no DB."""

from __future__ import annotations

from orchestrator.messenger_router.picker import parse_pick, render_picker


def test_parse_pick_numeric_id():
    assert parse_pick("42", active_task_ids=[42, 57]) == ("task", 42)


def test_parse_pick_hash_prefix():
    assert parse_pick("#42", active_task_ids=[42, 57]) == ("task", 42)


def test_parse_pick_with_whitespace():
    assert parse_pick("  42  ", active_task_ids=[42]) == ("task", 42)


def test_parse_pick_rejects_unknown_task_id():
    # 99 isn't in the active list — not a pick.
    assert parse_pick("99", active_task_ids=[42, 57]) is None


def test_parse_pick_new_starts_draft():
    assert parse_pick("new", active_task_ids=[1, 2]) == ("draft", None)
    assert parse_pick("NEW", active_task_ids=[1, 2]) == ("draft", None)
    assert parse_pick("  new ", active_task_ids=[1, 2]) == ("draft", None)


def test_parse_pick_returns_none_for_prose():
    assert parse_pick("hey what about task 42", active_task_ids=[42]) is None
    assert parse_pick("create a task", active_task_ids=[]) is None


def test_render_picker_includes_active_tasks_and_ids():
    text = render_picker(
        [
            {"id": 42, "title": "fix freeform PR rebase", "status": "awaiting_approval"},
            {"id": 57, "title": "add /test placeholder route", "status": "coding"},
        ]
    )
    assert "#42" in text
    assert "#57" in text
    assert "freeform PR rebase" in text
    assert "new" in text.lower()


def test_render_picker_with_no_active_tasks_offers_new_only():
    text = render_picker([])
    assert "new" in text.lower()
