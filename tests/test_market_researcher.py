"""Tests for agent/market_researcher.py.

These tests are DB-less unit tests that monkeypatch session methods and the
agent/workspace dependencies. Real-DB integration tests would require
DATABASE_URL to be set; they are intentionally out of scope here since the
MarketBrief model persistence is already covered by Task 4's model tests.

Key facts about the actual AgentResult shape (from agent/loop.py):
  - AgentResult.output: str  (last assistant message text)
  - AgentResult.messages: list[Message]  (full conversation history)
  - AgentResult.tool_calls_made: int  (total tool calls executed)
  - AgentResult.tokens_used: TokenUsage
  AgentResult does NOT expose workspace_state or turns — those are loop-local.
  URL fetches are extracted by scanning AgentResult.messages for assistant
  tool_calls with name == "fetch_url".
  Turn count is derived from the number of assistant messages.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.llm.types import Message, TokenUsage, ToolCall

# ---------------------------------------------------------------------------
# Helpers to build realistic AgentResult-like objects
# ---------------------------------------------------------------------------


def _make_tool_call(name: str, url: str) -> ToolCall:
    return ToolCall(id=f"tc-{url[:8]}", name=name, arguments={"url": url})


def _make_agent_result(output: str, fetched_urls: list[str]) -> SimpleNamespace:
    """Build a fake AgentResult that mirrors the real dataclass shape.

    Assistant messages are interleaved with tool messages to look realistic.
    fetch_url tool_calls appear on the assistant messages.
    """
    messages: list[Message] = [
        Message(role="user", content="do research"),
    ]
    if fetched_urls:
        tool_calls = [_make_tool_call("fetch_url", u) for u in fetched_urls]
        messages.append(
            Message(role="assistant", content="fetching…", tool_calls=tool_calls)
        )
        for tc in tool_calls:
            messages.append(
                Message(
                    role="tool",
                    content="<html>...</html>",
                    tool_call_id=tc.id,
                    tool_name="fetch_url",
                )
            )
    messages.append(Message(role="assistant", content=output, tool_calls=None))
    return SimpleNamespace(
        output=output,
        messages=messages,
        tool_calls_made=len(fetched_urls),
        tokens_used=TokenUsage(input_tokens=1000, output_tokens=200),
    )


@pytest.fixture
def fake_brief_output():
    return json.dumps({
        "product_category": "AI dev tools",
        "competitors": [
            {"name": "Cursor", "url": "https://cursor.com", "why_relevant": "AI IDE"},
        ],
        "findings": [
            {
                "theme": "agents",
                "observation": "multi-agent rising",
                "sources": ["https://cursor.com/blog/agents"],
            },
        ],
        "modality_gaps": [
            {
                "modality": "voice",
                "opportunity": "no voice yet",
                "sources": ["https://cursor.com"],
            },
        ],
        "strategic_themes": [
            {
                "theme": "AI-native",
                "why_now": "post-GPT-5",
                "sources": ["https://cursor.com"],
            },
        ],
        "summary": "Multi-agent and voice are rising in AI dev tools.",
    })


@pytest.fixture
def fake_repo():
    return SimpleNamespace(
        id=42,
        name="acme/backend",
        url="https://github.com/acme/backend",
        default_branch="main",
    )


@pytest.fixture
def fake_config():
    return SimpleNamespace(
        repo_id=42,
        organization_id=7,
        enabled=True,
        dev_branch="dev",
        last_market_research_at=None,
    )


def _make_session(brief_id: int = 99) -> MagicMock:
    """Return a mock AsyncSession.

    session.flush() is async and sets brief.id so callers can publish it.
    """
    session = MagicMock()
    session.add = MagicMock()

    async def _flush():
        pass

    session.flush = _flush
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_researcher_writes_brief_with_raw_sources(
    fake_repo, fake_config, fake_brief_output
):
    """Happy-path: agent produces valid JSON, URLs are extracted from tool calls."""
    fetched = ["https://cursor.com", "https://cursor.com/blog/agents"]
    agent_result = _make_agent_result(fake_brief_output, fetched)

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=agent_result)
    session = _make_session()

    import agent.market_researcher as mr

    with (
        patch.object(mr, "create_agent", return_value=fake_agent),
        patch.object(mr, "clone_repo", new=AsyncMock(return_value="/tmp/fake-ws")),
    ):
        brief = await mr.run_market_research(session, fake_config, fake_repo)

    assert brief is not None
    assert brief.product_category == "AI dev tools"
    assert len(brief.raw_sources) == 2
    urls = {s["url"] for s in brief.raw_sources}
    assert urls == {"https://cursor.com", "https://cursor.com/blog/agents"}
    assert brief.partial is False
    # 2 assistant messages (tool-call turn + final text turn)
    assert brief.agent_turns == 2
    session.add.assert_called_once_with(brief)
    # config timestamp updated
    assert fake_config.last_market_research_at is not None


@pytest.mark.asyncio
async def test_researcher_deduplicates_urls(fake_repo, fake_config, fake_brief_output):
    """Duplicate URLs from multiple tool calls should only appear once."""
    duplicate_fetched = ["https://cursor.com", "https://cursor.com"]  # fetched twice
    agent_result = _make_agent_result(fake_brief_output, duplicate_fetched)

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=agent_result)
    session = _make_session()

    import agent.market_researcher as mr

    with (
        patch.object(mr, "create_agent", return_value=fake_agent),
        patch.object(mr, "clone_repo", new=AsyncMock(return_value="/tmp/fake-ws")),
    ):
        brief = await mr.run_market_research(session, fake_config, fake_repo)

    assert brief is not None
    assert len(brief.raw_sources) == 1


@pytest.mark.asyncio
async def test_researcher_returns_none_when_output_unparseable(fake_repo, fake_config):
    """Non-JSON agent output → return None, don't persist a row."""
    agent_result = _make_agent_result("this is not json", [])

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=agent_result)
    session = _make_session()

    import agent.market_researcher as mr

    with (
        patch.object(mr, "create_agent", return_value=fake_agent),
        patch.object(mr, "clone_repo", new=AsyncMock(return_value="/tmp/fake-ws")),
    ):
        brief = await mr.run_market_research(session, fake_config, fake_repo)

    assert brief is None
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_researcher_marks_partial_when_output_is_empty_json(
    fake_repo, fake_config
):
    """Empty JSON {} → persist a row with partial=True."""
    agent_result = _make_agent_result("{}", [])

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=agent_result)
    session = _make_session()

    import agent.market_researcher as mr

    with (
        patch.object(mr, "create_agent", return_value=fake_agent),
        patch.object(mr, "clone_repo", new=AsyncMock(return_value="/tmp/fake-ws")),
    ):
        brief = await mr.run_market_research(session, fake_config, fake_repo)

    assert brief is not None
    assert brief.partial is True
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_researcher_returns_none_on_clone_failure(fake_repo, fake_config):
    """Clone failure → return None immediately without calling the agent."""
    session = _make_session()

    import agent.market_researcher as mr

    async def _fail(*a, **kw):
        raise RuntimeError("git clone failed")

    with patch.object(mr, "clone_repo", new=_fail):
        brief = await mr.run_market_research(session, fake_config, fake_repo)

    assert brief is None
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_researcher_returns_none_on_agent_exception(fake_repo, fake_config):
    """Agent run raising an exception → return None, event published."""
    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(side_effect=RuntimeError("LLM error"))
    session = _make_session()

    import agent.market_researcher as mr

    with (
        patch.object(mr, "create_agent", return_value=fake_agent),
        patch.object(mr, "clone_repo", new=AsyncMock(return_value="/tmp/fake-ws")),
    ):
        brief = await mr.run_market_research(session, fake_config, fake_repo)

    assert brief is None
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_researcher_uses_config_organization_id(
    fake_repo, fake_config, fake_brief_output
):
    """MarketBrief.organization_id must come from config, not repo."""
    fake_config.organization_id = 999
    agent_result = _make_agent_result(fake_brief_output, [])

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=agent_result)
    session = _make_session()

    import agent.market_researcher as mr

    with (
        patch.object(mr, "create_agent", return_value=fake_agent),
        patch.object(mr, "clone_repo", new=AsyncMock(return_value="/tmp/fake-ws")),
    ):
        brief = await mr.run_market_research(session, fake_config, fake_repo)

    assert brief is not None
    assert brief.organization_id == 999
