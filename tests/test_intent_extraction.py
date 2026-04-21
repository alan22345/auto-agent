"""Tests for intent extraction and structured intent fields."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.main import extract_intent
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


class TestExtractIntent:
    @pytest.mark.asyncio
    async def test_extract_intent_parses_json(self):
        """extract_intent should return parsed intent fields from LLM JSON output."""
        mock_response = MagicMock()
        mock_response.message.content = json.dumps({
            "change_type": "bugfix",
            "target_areas": "auth/login.py",
            "acceptance_criteria": "Login works on mobile",
            "constraints": "Don't change session schema",
        })

        mock_provider = AsyncMock()
        mock_provider.complete.return_value = mock_response

        with patch("agent.main.get_provider", return_value=mock_provider):
            result = await extract_intent("Fix login bug", "Login fails on mobile devices")

        assert result["change_type"] == "bugfix"
        assert result["target_areas"] == "auth/login.py"
        assert result["acceptance_criteria"] == "Login works on mobile"
        assert result["constraints"] == "Don't change session schema"

    @pytest.mark.asyncio
    async def test_extract_intent_returns_empty_on_failure(self):
        """If LLM call fails, return empty dict — don't block the pipeline."""
        mock_provider = AsyncMock()
        mock_provider.complete.side_effect = Exception("LLM down")

        with patch("agent.main.get_provider", return_value=mock_provider):
            result = await extract_intent("Fix login bug", "Login fails on mobile")

        assert result == {}

    @pytest.mark.asyncio
    async def test_extract_intent_returns_empty_on_invalid_json(self):
        """If LLM returns non-JSON, return empty dict."""
        mock_response = MagicMock()
        mock_response.message.content = "I think this is a bugfix for the login page"

        mock_provider = AsyncMock()
        mock_provider.complete.return_value = mock_response

        with patch("agent.main.get_provider", return_value=mock_provider):
            result = await extract_intent("Fix login bug", "Login fails on mobile")

        assert result == {}
