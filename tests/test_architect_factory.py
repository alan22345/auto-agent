"""Tests for the trio architect prompts and factory.

The factory test monkeypatches ``create_agent`` inside the architect module so
that no LLM provider credentials are needed — the test focuses purely on the
tool-registry composition that the factory applies to the returned AgentLoop.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.lifecycle.trio.architect import create_architect_agent
from agent.lifecycle.trio.prompts import (
    ARCHITECT_CHECKPOINT_SYSTEM,
    ARCHITECT_CONSULT_SYSTEM,
    ARCHITECT_INITIAL_SYSTEM,
)


def test_initial_prompt_mentions_architecture_md_and_backlog():
    assert "ARCHITECTURE.md" in ARCHITECT_INITIAL_SYSTEM
    assert "backlog" in ARCHITECT_INITIAL_SYSTEM.lower()


def test_initial_prompt_steers_freeform_autonomy():
    assert "freeform" in ARCHITECT_INITIAL_SYSTEM.lower()
    assert "record_decision" in ARCHITECT_INITIAL_SYSTEM


def test_consult_prompt_mentions_focused_question():
    assert "question" in ARCHITECT_CONSULT_SYSTEM.lower()


def test_checkpoint_prompt_mentions_continue_revise_done():
    body = ARCHITECT_CHECKPOINT_SYSTEM.lower()
    assert "continue" in body and "revise" in body and "done" in body


def test_create_architect_agent_returns_loop_with_architect_tools(tmp_path):
    """Factory must attach record_decision + request_market_brief, NOT consult_architect."""
    # Stub out create_agent so no LLM provider credentials are required.
    # We return a MagicMock whose .tools attribute the factory will replace with
    # a real ToolRegistry via agent.tools = create_default_registry(...).
    stub_agent = MagicMock()

    with patch(
        "agent.lifecycle.trio.architect.create_agent", return_value=stub_agent
    ):
        agent = create_architect_agent(
            workspace=str(tmp_path),
            task_id=42,
            task_description="Build a recipe app",
            phase="initial",
        )

    # The factory replaces .tools with a real ToolRegistry — inspect it.
    assert agent.tools.get("record_decision") is not None
    assert agent.tools.get("request_market_brief") is not None
    # Architect must NOT have consult_architect.
    assert agent.tools.get("consult_architect") is None
