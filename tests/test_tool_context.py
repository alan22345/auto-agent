import pytest

from agent.tools.base import ToolContext


@pytest.mark.asyncio
async def test_event_sink_callable_invoked_when_set():
    received: list[dict] = []

    async def sink(event: dict) -> None:
        received.append(event)

    ctx = ToolContext(workspace="/tmp", event_sink=sink)
    assert ctx.event_sink is not None
    await ctx.event_sink({"type": "source", "url": "https://example.com"})
    assert received == [{"type": "source", "url": "https://example.com"}]


def test_event_sink_default_is_none():
    ctx = ToolContext(workspace="/tmp")
    assert ctx.event_sink is None
