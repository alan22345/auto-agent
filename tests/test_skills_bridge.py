"""Spec for the auto-agent skills bridge — ADR-015 §12.

Every gated agent action is a *skill* vendored at
``skills/auto-agent/<skill-name>/SKILL.md``. Each skill tells CC to write
JSON/markdown to a known workspace path and stop. The orchestrator reads
the file after ``agent.run`` returns.

This test pins:

1. Each skill directory exists under ``skills/auto-agent/``.
2. ``SKILL.md`` has the standard skill frontmatter — a ``name`` field and
   a ``description`` field.
3. The skill body references the workspace path it must write to (via the
   constants in :mod:`agent.lifecycle.workspace_paths`).
4. JSON-emitting skills mention ``schema_version`` so CC writes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.lifecycle import workspace_paths as wp

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = REPO_ROOT / "skills" / "auto-agent"


# (skill_name, target_path_string, kind)
# ``kind`` is "json" or "markdown" — JSON skills must mention schema_version.
# Slice skills use a literal token in their target string because <name> is
# substituted at invocation time.
SKILLS: list[tuple[str, str, str]] = [
    ("submit-grill-exit", wp.GRILL_PATH, "json"),
    ("submit-plan", wp.PLAN_PATH, "markdown"),
    ("submit-design", wp.DESIGN_PATH, "markdown"),
    ("submit-backlog", wp.BACKLOG_PATH, "json"),
    ("submit-architect-decision", wp.DECISION_PATH, "json"),
    ("submit-item-review", ".auto-agent/reviews/", "json"),  # item_id template
    ("submit-final-review", wp.FINAL_REVIEW_PATH, "json"),
    ("submit-pr-review", wp.PR_REVIEW_PATH, "json"),
    ("submit-grill-question", ".auto-agent/slices/", "json"),  # <name> template
    ("submit-grill-answer", ".auto-agent/slices/", "json"),
]


@pytest.mark.parametrize("skill_name,target_path,kind", SKILLS)
def test_skill_directory_and_file_exist(skill_name: str, target_path: str, kind: str) -> None:
    skill_dir = SKILLS_ROOT / skill_name
    assert skill_dir.is_dir(), f"missing skill directory: {skill_dir}"
    assert (skill_dir / "SKILL.md").is_file(), f"missing SKILL.md in {skill_dir}"


@pytest.mark.parametrize("skill_name,target_path,kind", SKILLS)
def test_skill_has_name_and_description_frontmatter(
    skill_name: str, target_path: str, kind: str
) -> None:
    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text()
    # YAML frontmatter delimiters
    assert text.startswith("---"), f"{skill_name}: missing leading frontmatter fence"
    head, _, _ = text[3:].partition("---")
    assert f"name: {skill_name}" in head, f"{skill_name}: frontmatter missing name field"
    assert "description:" in head, f"{skill_name}: frontmatter missing description field"


@pytest.mark.parametrize("skill_name,target_path,kind", SKILLS)
def test_skill_body_references_workspace_path(skill_name: str, target_path: str, kind: str) -> None:
    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text()
    assert target_path in text, (
        f"{skill_name}: SKILL.md does not reference the workspace path {target_path}"
    )


@pytest.mark.parametrize("skill_name,target_path,kind", SKILLS)
def test_json_skills_mention_schema_version(skill_name: str, target_path: str, kind: str) -> None:
    if kind != "json":
        pytest.skip("markdown skill — no schema_version needed")
    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text()
    assert "schema_version" in text, f"{skill_name}: JSON skill must emit schema_version"
    assert '"1"' in text or "'1'" in text, f'{skill_name}: schema_version must literally be "1"'


@pytest.mark.parametrize("skill_name,target_path,kind", SKILLS)
def test_skill_body_instructs_stop_after_write(
    skill_name: str, target_path: str, kind: str
) -> None:
    """Each skill must tell CC to stop after writing the file — the
    orchestrator owns the next step."""

    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text().lower()
    # Be permissive on phrasing — pin only that the intent is present.
    assert "stop" in text, f"{skill_name}: SKILL.md must instruct CC to stop after the write"
