from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.consult_architect import ConsultArchitectTool


@pytest.mark.asyncio
async def test_consult_architect_calls_architect_module_with_parent_id():
    tool = ConsultArchitectTool()
    ctx = ToolContext(workspace="/tmp", task_id=99, parent_task_id=42)

    with patch("agent.tools.consult_architect.architect_consult", new=AsyncMock(return_value="Yes, use Postgres.")) as m:
        result = await tool.execute(
            {"question": "Which db?", "why": "Need to choose between Postgres and SQLite."},
            ctx,
        )

    m.assert_awaited_once()
    args = m.await_args.kwargs
    assert args["parent_task_id"] == 42
    assert args["child_task_id"] == 99
    assert args["question"] == "Which db?"
    assert args["why"] == "Need to choose between Postgres and SQLite."
    assert "Yes, use Postgres." in result.output


@pytest.mark.asyncio
async def test_consult_architect_rejects_when_not_a_trio_child():
    tool = ConsultArchitectTool()
    ctx = ToolContext(workspace="/tmp", task_id=99, parent_task_id=None)
    result = await tool.execute({"question": "x", "why": "x"}, ctx)
    assert result.is_error
    assert "trio child" in result.output.lower()


@pytest.mark.asyncio
async def test_consult_architect_surfaces_doc_update_note():
    tool = ConsultArchitectTool()
    ctx = ToolContext(workspace="/tmp", task_id=99, parent_task_id=42)

    return_payload = {
        "answer": "Use Postgres.",
        "architecture_md_updated": True,
    }
    with patch("agent.tools.consult_architect.architect_consult", new=AsyncMock(return_value=return_payload)):
        result = await tool.execute({"question": "x", "why": "x"}, ctx)

    assert "ARCHITECTURE.md was updated" in result.output
    assert "Use Postgres." in result.output
