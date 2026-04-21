"""Tests for intent extraction and structured intent fields."""

from __future__ import annotations

from shared.types import TaskData


class TestTaskDataIntentFields:
    def test_intent_fields_have_defaults(self):
        """New intent fields should be optional with None defaults."""
        task = TaskData(
            id=1, title="Fix login bug", description="Login fails on mobile",
            source="manual", status="created",
        )
        assert task.change_type is None
        assert task.target_areas is None
        assert task.acceptance_criteria is None
        assert task.constraints is None

    def test_intent_fields_populated(self):
        """Intent fields can be set explicitly."""
        task = TaskData(
            id=1, title="Fix login bug", description="Login fails on mobile",
            source="manual", status="created",
            change_type="bugfix",
            target_areas="auth/login.py, auth/session.py",
            acceptance_criteria="Login works on mobile browsers, existing tests pass",
            constraints="Do not change the session schema",
        )
        assert task.change_type == "bugfix"
        assert task.target_areas == "auth/login.py, auth/session.py"
        assert task.acceptance_criteria == "Login works on mobile browsers, existing tests pass"
        assert task.constraints == "Do not change the session schema"
