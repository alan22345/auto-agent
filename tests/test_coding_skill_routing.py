"""Tests for change_type → skill_directive routing in build_coding_prompt.

The agent's coding prompts auto-load `diagnose` for bugfix tasks and
`tdd` + `improve-codebase-architecture` for feature/refactor/perf tasks.
The architecture lens (deletion test, deep modules, seams) is permanently
present in the critical rules block.
"""

from __future__ import annotations

import pytest

from agent.context.system import BASE_AGENT_INSTRUCTIONS, METHODOLOGY_INSTRUCTIONS
from agent.prompts import _CRITICAL_RULES, _skill_directive, build_coding_prompt


@pytest.mark.parametrize(
    "change_type,expected_skill",
    [
        ("bugfix", "diagnose"),
        ("feature", "tdd"),
        ("refactor", "tdd"),
        ("performance", "tdd"),
    ],
)
def test_skill_directive_routes_by_change_type(change_type, expected_skill):
    directive = _skill_directive({"change_type": change_type})
    assert expected_skill in directive
    if change_type != "bugfix":
        # Feature/refactor/perf must also load improve-codebase-architecture
        assert "improve-codebase-architecture" in directive


@pytest.mark.parametrize("change_type", ["docs", "config", "test", "", None])
def test_skill_directive_empty_for_other_types(change_type):
    directive = _skill_directive({"change_type": change_type} if change_type is not None else None)
    assert directive == ""


def test_build_coding_prompt_injects_diagnose_for_bugfix():
    prompt = build_coding_prompt(
        title="Fix null deref in webhook handler",
        description="...",
        intent={"change_type": "bugfix"},
    )
    assert "skill(name='diagnose')" in prompt


def test_build_coding_prompt_injects_tdd_for_feature():
    prompt = build_coding_prompt(
        title="Add stripe webhook handler",
        description="...",
        intent={"change_type": "feature"},
    )
    assert "skill(name='tdd')" in prompt
    assert "improve-codebase-architecture" in prompt


def test_build_coding_prompt_no_skill_directive_for_docs():
    prompt = build_coding_prompt(
        title="Update README",
        description="...",
        intent={"change_type": "docs"},
    )
    # No skill auto-load for docs/config/test
    assert "skill(name='diagnose')" not in prompt
    assert "skill(name='tdd')" not in prompt


def test_critical_rules_contains_architecture_section():
    """The deepening lens is permanently in _CRITICAL_RULES — applies to every coding task."""
    assert "### Architecture (deepening lens)" in _CRITICAL_RULES
    assert "deletion test" in _CRITICAL_RULES.lower()
    assert "deep modules" in _CRITICAL_RULES.lower()
    assert "two adapters" in _CRITICAL_RULES.lower() or "real seam" in _CRITICAL_RULES.lower()


def test_base_agent_instructions_contains_architecture_lens():
    """Architecture vocabulary is in the system prompt itself, so it's always loaded."""
    assert "## Architecture (mandatory lens)" in BASE_AGENT_INSTRUCTIONS
    assert "deletion test" in BASE_AGENT_INSTRUCTIONS.lower()
    assert "seam" in BASE_AGENT_INSTRUCTIONS.lower()
    assert "leverage" in BASE_AGENT_INSTRUCTIONS.lower()
    assert "locality" in BASE_AGENT_INSTRUCTIONS.lower()


def test_skills_section_lists_engineering_skills():
    """The skills list in the system prompt names the engineering skills."""
    for name in ("grill-with-docs", "improve-codebase-architecture", "tdd", "diagnose"):
        assert f"**{name}**" in BASE_AGENT_INSTRUCTIONS, f"{name} not in skills list"


def test_methodology_lens_paragraph():
    """The extended methodology block leads with the architecture-lens framing."""
    assert "improve-codebase-architecture" in METHODOLOGY_INSTRUCTIONS
    assert "grill-with-docs" in METHODOLOGY_INSTRUCTIONS


def test_build_coding_prompt_no_intent_renders_cleanly():
    """Backwards compatibility: no intent means no skill_directive — but still renders."""
    prompt = build_coding_prompt(
        title="Some task",
        description="...",
        intent=None,
    )
    # Template substituted without errors and no leftover placeholder
    assert "{skill_directive}" not in prompt
    assert "skill(name=" not in prompt
