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


class TestCodingPromptWithIntent:
    def test_coding_prompt_includes_intent_section(self):
        """When intent dict is provided, the coding prompt should include a structured intent section."""
        from agent.prompts import build_coding_prompt

        intent = {
            "change_type": "bugfix",
            "target_areas": "auth/login.py",
            "acceptance_criteria": "Login works on mobile",
            "constraints": "Don't change session schema",
        }
        prompt = build_coding_prompt(
            title="Fix login bug",
            description="Login fails on mobile",
            intent=intent,
        )
        assert "## Structured intent" in prompt
        assert "bugfix" in prompt
        assert "auth/login.py" in prompt
        assert "Login works on mobile" in prompt
        assert "Don't change session schema" in prompt

    def test_coding_prompt_without_intent(self):
        """When no intent is provided, the prompt should still work without an intent section."""
        from agent.prompts import build_coding_prompt

        prompt = build_coding_prompt(
            title="Fix login bug",
            description="Login fails on mobile",
        )
        assert "## Structured intent" not in prompt
        assert "## Task" in prompt

    def test_coding_prompt_without_plan_has_restate_step(self):
        """When no plan is provided, the coding prompt should ask the agent to restate the task."""
        from agent.prompts import build_coding_prompt

        prompt = build_coding_prompt(
            title="Fix login bug",
            description="Login fails on mobile",
        )
        assert "restate" in prompt.lower() or "summarize what you" in prompt.lower()

    def test_coding_prompt_with_plan_no_restate(self):
        """When a plan is provided, the coding prompt should use the immediate variant."""
        from agent.prompts import build_coding_prompt

        prompt = build_coding_prompt(
            title="Fix login bug",
            description="Login fails on mobile",
            plan="## Phase 1\nDo the thing",
        )
        assert "IMMEDIATELY" in prompt


class TestStructuredPlanningPrompt:
    def test_planning_prompt_requires_goal_section(self):
        """Planning prompt should instruct agent to include a Goal section."""
        from agent.prompts import build_planning_prompt
        prompt = build_planning_prompt("Add dark mode", "Support dark mode toggle in settings")
        assert "## Goal" in prompt or "### Goal" in prompt

    def test_planning_prompt_requires_acceptance_criteria(self):
        """Planning prompt should instruct agent to include acceptance criteria."""
        from agent.prompts import build_planning_prompt
        prompt = build_planning_prompt("Add dark mode", "Support dark mode toggle in settings")
        assert "acceptance criteria" in prompt.lower() or "Acceptance Criteria" in prompt

    def test_planning_prompt_requires_files_to_modify(self):
        """Planning prompt should instruct agent to list files to modify."""
        from agent.prompts import build_planning_prompt
        prompt = build_planning_prompt("Add dark mode", "Support dark mode toggle in settings")
        assert "files" in prompt.lower() and "modify" in prompt.lower()
