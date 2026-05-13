from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.request_market_brief import RequestMarketBriefTool


@pytest.mark.asyncio
async def test_request_market_brief_invokes_researcher():
    tool = RequestMarketBriefTool()
    ctx = ToolContext(workspace="/tmp", task_id=42)
    brief = "Recipe apps in 2026 favor voice-first..."

    with patch(
        "agent.tools.request_market_brief.run_market_research",
        new=AsyncMock(return_value={"brief_id": 7, "summary": brief}),
    ) as m:
        result = await tool.execute(
            {"product_description": "voice-driven recipe app"},
            ctx,
        )

    m.assert_awaited_once()
    assert m.await_args.kwargs["task_id"] == 42
    assert brief in result.output


@pytest.mark.asyncio
async def test_request_market_brief_needs_task_id():
    tool = RequestMarketBriefTool()
    ctx = ToolContext(workspace="/tmp", task_id=None)
    result = await tool.execute({"product_description": "x"}, ctx)
    assert result.is_error
