"""Tests for skills/engineering vendoring + multi-directory discovery.

Engineering skills live at ``skills/engineering/`` and take precedence over
``superpowers/skills/`` on name collision (see ``agent/tools/skill.py``).
"""

from __future__ import annotations

import importlib
import os

import pytest


def test_engineering_skills_discovered():
    """All vendored Pocock engineering skills load with non-empty descriptions."""
    from agent.tools import skill

    # Re-import so we read the on-disk state, not stale state from another test.
    importlib.reload(skill)

    expected = {
        "diagnose",
        "grill-with-docs",
        "improve-codebase-architecture",
        "tdd",
        "zoom-out",
        "triage",
        "to-prd",
        "to-issues",
    }
    missing = expected - set(skill.AVAILABLE_SKILLS)
    assert not missing, f"Engineering skills not discovered: {missing}"

    for name in expected:
        info = skill.AVAILABLE_SKILLS[name]
        assert info["description"], f"Skill {name!r} has no description"
        assert "skills/engineering/" in info["dir"], (
            f"Skill {name!r} resolved to {info['dir']!r}, expected engineering root"
        )
        assert os.path.isfile(info["path"]), f"SKILL.md missing for {name}"


def test_skill_tool_enum_contains_engineering_skills():
    """The tool's parameter enum lists engineering skills, so the LLM can name them."""
    from agent.tools import skill

    importlib.reload(skill)
    tool = skill.SkillTool()
    enum_names = set(tool.parameters["properties"]["name"]["enum"])
    for n in ("grill-with-docs", "improve-codebase-architecture", "tdd", "diagnose"):
        assert n in enum_names, f"{n!r} missing from skill tool enum"


def test_engineering_takes_precedence_on_collision(tmp_path, monkeypatch):
    """When a skill name exists in both roots, the engineering root wins."""
    from agent.tools import skill

    # Build two fake skill roots, each with a skill called 'grill-with-docs'.
    eng_root = tmp_path / "engineering"
    sup_root = tmp_path / "superpowers"
    (eng_root / "grill-with-docs").mkdir(parents=True)
    (sup_root / "grill-with-docs").mkdir(parents=True)

    (eng_root / "grill-with-docs" / "SKILL.md").write_text(
        "---\nname: grill-with-docs\ndescription: ENGINEERING VERSION\n---\n\nbody"
    )
    (sup_root / "grill-with-docs" / "SKILL.md").write_text(
        "---\nname: grill-with-docs\ndescription: SUPERPOWERS VERSION\n---\n\nbody"
    )

    monkeypatch.setattr(skill, "_SKILL_DIRS", [str(eng_root), str(sup_root)])
    discovered = skill._discover_skills()
    assert "grill-with-docs" in discovered
    assert discovered["grill-with-docs"]["description"] == "ENGINEERING VERSION"
    assert str(eng_root) in discovered["grill-with-docs"]["dir"]


def test_supplementary_files_loaded_for_engineering_skill():
    """Loading 'improve-codebase-architecture' should include its sibling .md files."""
    from agent.tools import skill

    importlib.reload(skill)
    info = skill.AVAILABLE_SKILLS["improve-codebase-architecture"]

    # Collect siblings that the SkillTool would include.
    siblings = sorted(
        f for f in os.listdir(info["dir"]) if f.endswith(".md") and f != "SKILL.md"
    )
    assert "LANGUAGE.md" in siblings
    assert "DEEPENING.md" in siblings


@pytest.mark.asyncio
async def test_skill_execute_returns_full_content_with_supplementary():
    """Calling SkillTool.execute('improve-codebase-architecture') returns SKILL.md +
    supplementary files concatenated."""
    from agent.tools import skill
    from agent.tools.base import ToolContext

    importlib.reload(skill)
    tool = skill.SkillTool()
    ctx = ToolContext(workspace=os.getcwd())
    result = await tool.execute({"name": "improve-codebase-architecture"}, ctx)

    assert not result.is_error, result.output
    # SKILL.md content
    assert "deepening opportunities" in result.output.lower()
    # Supplementary file content (DEEPENING/LANGUAGE/INTERFACE-DESIGN should appear)
    assert "## DEEPENING.md" in result.output or "## LANGUAGE.md" in result.output
