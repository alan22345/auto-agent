import re

import pytest

from agent.tools.base import ToolContext
from agent.tools.record_decision import RecordDecisionTool


@pytest.mark.asyncio
async def test_record_decision_creates_numbered_adr(tmp_path):
    # Pre-existing template + one ADR so the tool has to pick number 002.
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "docs" / "decisions" / "000-template.md").write_text(
        "# {title}\n\n## Context\n{context}\n\n## Decision\n{decision}\n\n## Consequences\n{consequences}\n"
    )
    (tmp_path / "docs" / "decisions" / "001-existing.md").write_text("# Existing\n")

    tool = RecordDecisionTool()
    ctx = ToolContext(workspace=str(tmp_path), task_id=42)
    result = await tool.execute(
        {
            "title": "Use Postgres over SQLite",
            "context": "Multi-user implied by task description.",
            "decision": "Postgres.",
            "consequences": "Heavier dependency; required for concurrent writes.",
        },
        ctx,
    )

    assert not result.is_error
    # Result output should contain the path the architect can reference.
    assert "002-use-postgres-over-sqlite.md" in result.output

    written = tmp_path / "docs" / "decisions" / "002-use-postgres-over-sqlite.md"
    assert written.exists()
    body = written.read_text()
    assert "Use Postgres over SQLite" in body
    assert "Multi-user implied" in body


@pytest.mark.asyncio
async def test_record_decision_slug_sanitises(tmp_path):
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "docs" / "decisions" / "000-template.md").write_text(
        "# {title}\n{context}\n{decision}\n{consequences}\n"
    )

    tool = RecordDecisionTool()
    ctx = ToolContext(workspace=str(tmp_path), task_id=42)
    result = await tool.execute(
        {
            "title": "Use FastAPI / Next.js (full-stack)",
            "context": "x",
            "decision": "y",
            "consequences": "z",
        },
        ctx,
    )

    assert not result.is_error
    # Slashes, parens stripped; spaces → hyphens; lowercased; <=40 chars.
    assert re.search(r"001-use-fastapi-next-js-full-stack", result.output)


@pytest.mark.asyncio
async def test_record_decision_missing_template_errors(tmp_path):
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    tool = RecordDecisionTool()
    ctx = ToolContext(workspace=str(tmp_path), task_id=42)
    result = await tool.execute(
        {"title": "x", "context": "x", "decision": "x", "consequences": "x"},
        ctx,
    )
    assert result.is_error
    assert "template" in result.output.lower()
