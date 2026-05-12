"""Integration tests for the researcher → PO chain in agent/po_analyzer.py.

These are DB-less unit tests. Rather than trying to mock the full SQLAlchemy
execute() chain (which is complex and brittle), we use a hybrid approach:

- Tests for `_filter_grounded` and `_brief_is_fresh` are pure unit tests.
- Tests for `_ensure_brief` patch `run_market_research` and stub session.execute
  with a helper.
- Tests for `handle_po_analysis` patch `_ensure_brief` or call it directly with
  a fake brief object.
- The full-chain scenario (`_check_and_analyze`) is tested by calling
  `_ensure_brief` + `handle_po_analysis` directly with fully mocked deps,
  since `_check_and_analyze` itself loops over DB rows which cannot be
  reasonably stubbed without a real DB.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.llm.types import Message, TokenUsage, ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_call(name: str, url: str) -> ToolCall:
    return ToolCall(id=f"tc-{url[:8]}", name=name, arguments={"url": url})


def _make_agent_result(output: str, fetched_urls: list[str] | None = None) -> SimpleNamespace:
    fetched_urls = fetched_urls or []
    messages: list[Message] = [Message(role="user", content="do research")]
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


def _make_brief(
    repo_id: int = 1,
    organization_id: int = 1,
    age_days: float = 0,
) -> SimpleNamespace:
    """Build a fake MarketBrief-like object."""
    created_at = datetime.now(UTC) - timedelta(days=age_days)
    return SimpleNamespace(
        id=99,
        repo_id=repo_id,
        organization_id=organization_id,
        product_category="AI",
        competitors=[{"name": "X", "url": "https://x.example", "why_relevant": "y"}],
        findings=[{"theme": "t", "observation": "o", "sources": ["https://x.example"]}],
        modality_gaps=[],
        strategic_themes=[],
        summary="Test summary.",
        partial=False,
        created_at=created_at,
    )


def _po_output_two_grounded() -> str:
    return json.dumps({
        "suggestions": [
            {
                "title": "Add voice",
                "description": "desc",
                "rationale": "rationale",
                "category": "feature",
                "priority": 2,
                "evidence_urls": [
                    {"url": "https://x.example", "title": "X", "excerpt": "voice"}
                ],
            },
            {
                "title": "Fix login crash",
                "description": "desc",
                "rationale": "rationale",
                "category": "bug",
                "priority": 1,
                "evidence_urls": [],
            },
        ],
        "ux_knowledge_update": "voice is important",
    })


def _po_output_one_ungrounded() -> str:
    """Feature with evidence + ux_gap without evidence (should be dropped)."""
    return json.dumps({
        "suggestions": [
            {
                "title": "Add voice",
                "description": "desc",
                "rationale": "rationale",
                "category": "feature",
                "priority": 2,
                "evidence_urls": [
                    {"url": "https://x.example", "title": "X", "excerpt": "voice"}
                ],
            },
            {
                "title": "Generic polish",
                "description": "desc",
                "rationale": "rationale",
                "category": "ux_gap",
                "priority": 3,
                "evidence_urls": [],
            },
        ],
        "ux_knowledge_update": None,
    })


def _make_fake_session(
    *,
    latest_brief=None,
    repo=None,
    on_add=None,
) -> MagicMock:
    """Return a mock AsyncSession supporting the queries _ensure_brief and
    handle_po_analysis issue.

    - select(MarketBrief).where(...).order_by(...).limit(1) → latest_brief
    - select(Repo).where(...) → repo
    - select(Suggestion.title).where(...).order_by(...).limit(50) → []
    - session.add(obj) calls on_add(obj)
    - session.flush() / session.commit() are no-ops
    """
    session = MagicMock()

    async def _flush():
        pass

    async def _commit():
        pass

    session.flush = _flush
    session.commit = _commit

    added_items = []

    def _add(obj):
        added_items.append(obj)
        if on_add:
            on_add(obj)

    session.add = _add

    async def _execute(stmt):
        """Return a result based on which query is being run.

        We detect the query type by inspecting the statement's entity or
        the columns being selected.
        """
        stmt_str = str(stmt)

        # Suggestion.title query (recent suggestions for PO prompt)
        if "suggestions" in stmt_str.lower() and "title" in stmt_str.lower():
            result = MagicMock()
            result.all = MagicMock(return_value=[])
            return result

        # MarketBrief query
        if "market_brief" in stmt_str.lower():
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=latest_brief)
            return result

        # Repo query
        if "repo" in stmt_str.lower():
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=repo)
            return result

        # FreeformConfig query (for _check_and_analyze)
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = _execute
    return session


def _make_fake_config(
    last_market_research_at=None,
    market_brief_max_age_days: int = 7,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        repo_id=1,
        organization_id=1,
        enabled=True,
        analysis_cron="* * * * *",
        last_analysis_at=None,
        last_market_research_at=last_market_research_at,
        market_brief_max_age_days=market_brief_max_age_days,
        dev_branch="main",
        ux_knowledge=None,
        po_goal=None,
    )


def _make_fake_repo() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        name="acme/backend",
        url="https://github.com/acme/backend",
        organization_id=1,
        default_branch="main",
    )


# ---------------------------------------------------------------------------
# Unit tests for _filter_grounded
# ---------------------------------------------------------------------------


def test_filter_grounded_keeps_bugs_without_evidence():
    from agent.po_analyzer import _filter_grounded

    suggestions = [
        {
            "title": "Fix crash",
            "category": "bug",
            "evidence_urls": [],
        }
    ]
    kept, dropped = _filter_grounded(suggestions)
    assert dropped == 0
    assert len(kept) == 1


def test_filter_grounded_keeps_features_with_evidence():
    from agent.po_analyzer import _filter_grounded

    suggestions = [
        {
            "title": "Add voice",
            "category": "feature",
            "evidence_urls": [{"url": "https://x.example", "title": "X", "excerpt": "v"}],
        }
    ]
    kept, dropped = _filter_grounded(suggestions)
    assert dropped == 0
    assert len(kept) == 1


def test_filter_grounded_drops_non_bug_without_evidence():
    from agent.po_analyzer import _filter_grounded

    suggestions = [
        {"title": "Generic polish", "category": "ux_gap", "evidence_urls": []},
        {"title": "Add voice", "category": "feature", "evidence_urls": []},
        {"title": "Fix crash", "category": "bug", "evidence_urls": []},
    ]
    kept, dropped = _filter_grounded(suggestions)
    assert dropped == 2
    assert len(kept) == 1
    assert kept[0]["title"] == "Fix crash"


def test_filter_grounded_mixed_scenario():
    from agent.po_analyzer import _filter_grounded

    suggestions = [
        {
            "title": "Add voice",
            "category": "feature",
            "priority": 2,
            "evidence_urls": [{"url": "https://x.example", "title": "X", "excerpt": "voice"}],
        },
        {
            "title": "Generic polish",
            "category": "ux_gap",
            "priority": 3,
            "evidence_urls": [],
        },
    ]
    kept, dropped = _filter_grounded(suggestions)
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0]["title"] == "Add voice"


def test_filter_grounded_empty_list():
    from agent.po_analyzer import _filter_grounded

    kept, dropped = _filter_grounded([])
    assert kept == []
    assert dropped == 0


def test_filter_grounded_null_evidence_urls():
    """None evidence_urls is treated as empty — non-bugs get dropped."""
    from agent.po_analyzer import _filter_grounded

    suggestions = [
        {"title": "Feature no evidence", "category": "feature", "evidence_urls": None},
    ]
    kept, dropped = _filter_grounded(suggestions)
    assert dropped == 1
    assert kept == []


# ---------------------------------------------------------------------------
# Unit tests for _brief_is_fresh
# ---------------------------------------------------------------------------


def test_brief_is_fresh_returns_true_for_young_brief():
    from agent.po_analyzer import _brief_is_fresh

    brief = _make_brief(age_days=3)
    assert _brief_is_fresh(brief, datetime.now(UTC), 7) is True


def test_brief_is_fresh_returns_false_for_old_brief():
    from agent.po_analyzer import _brief_is_fresh

    brief = _make_brief(age_days=10)
    assert _brief_is_fresh(brief, datetime.now(UTC), 7) is False


def test_brief_is_fresh_returns_false_for_none():
    from agent.po_analyzer import _brief_is_fresh

    assert _brief_is_fresh(None, datetime.now(UTC), 7) is False


# ---------------------------------------------------------------------------
# Tests for _ensure_brief
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_brief_returns_fresh_brief_without_researcher():
    """When a fresh brief exists, researcher is never called."""
    from agent import po_analyzer

    fresh_brief = _make_brief(age_days=2)
    config = _make_fake_config(market_brief_max_age_days=7)
    session = _make_fake_session(latest_brief=fresh_brief, repo=_make_fake_repo())

    researcher_called = {"count": 0}

    async def fake_run_market_research(*args, **kwargs):
        researcher_called["count"] += 1
        return None

    with patch("agent.po_analyzer.run_market_research", fake_run_market_research):
        result = await po_analyzer._ensure_brief(session, config)

    assert result is fresh_brief
    assert researcher_called["count"] == 0


@pytest.mark.asyncio
async def test_ensure_brief_calls_researcher_when_brief_stale():
    """Stale brief → researcher is called; new brief returned."""
    from agent import po_analyzer

    stale_brief = _make_brief(age_days=10)
    new_brief = _make_brief(age_days=0)
    repo = _make_fake_repo()
    config = _make_fake_config(market_brief_max_age_days=7)
    session = _make_fake_session(latest_brief=stale_brief, repo=repo)

    researcher_called = {"count": 0}

    async def fake_run_market_research(session, config, repo):
        researcher_called["count"] += 1
        return new_brief

    with patch("agent.po_analyzer.run_market_research", fake_run_market_research):
        result = await po_analyzer._ensure_brief(session, config)

    assert result is new_brief
    assert researcher_called["count"] == 1


@pytest.mark.asyncio
async def test_ensure_brief_falls_back_to_stale_on_researcher_failure():
    """Researcher returns None → fall back to prior stale brief."""
    from agent import po_analyzer

    stale_brief = _make_brief(age_days=10)
    repo = _make_fake_repo()
    config = _make_fake_config(market_brief_max_age_days=7)
    session = _make_fake_session(latest_brief=stale_brief, repo=repo)

    async def fake_run_market_research(*args, **kwargs):
        return None

    with patch("agent.po_analyzer.run_market_research", fake_run_market_research):
        result = await po_analyzer._ensure_brief(session, config)

    assert result is stale_brief


@pytest.mark.asyncio
async def test_ensure_brief_returns_none_when_no_brief_and_researcher_fails():
    """No prior brief and researcher fails → None returned."""
    from agent import po_analyzer

    repo = _make_fake_repo()
    config = _make_fake_config(market_brief_max_age_days=7)
    session = _make_fake_session(latest_brief=None, repo=repo)

    async def fake_run_market_research(*args, **kwargs):
        return None

    with patch("agent.po_analyzer.run_market_research", fake_run_market_research):
        result = await po_analyzer._ensure_brief(session, config)

    assert result is None


@pytest.mark.asyncio
async def test_ensure_brief_returns_none_when_no_brief_at_all():
    """No prior brief, no researcher called (repo missing) → None."""
    from agent import po_analyzer

    config = _make_fake_config(market_brief_max_age_days=7)
    # No repo → _ensure_brief returns latest (None) after researcher skipped
    session = _make_fake_session(latest_brief=None, repo=None)

    async def fake_run_market_research(*args, **kwargs):
        return None

    with patch("agent.po_analyzer.run_market_research", fake_run_market_research):
        result = await po_analyzer._ensure_brief(session, config)

    assert result is None


# ---------------------------------------------------------------------------
# Tests for handle_po_analysis with brief parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_po_analysis_persists_grounded_suggestions():
    """handle_po_analysis with a brief persists grounded suggestions."""
    from agent import po_analyzer

    brief = _make_brief()
    repo = _make_fake_repo()
    config = _make_fake_config()

    added_items: list = []
    session = _make_fake_session(
        latest_brief=brief,
        repo=repo,
        on_add=added_items.append,
    )

    po_agent = MagicMock()
    po_agent.run = AsyncMock(
        return_value=_make_agent_result(_po_output_two_grounded())
    )

    with (
        patch.object(po_analyzer, "create_agent", return_value=po_agent),
        patch.object(po_analyzer, "clone_repo", new=AsyncMock(return_value="/tmp/fake")),
        patch.object(po_analyzer, "remember_priority_suggestion", new=AsyncMock()),
        patch.object(po_analyzer, "publish", new=AsyncMock()),
    ):
        await po_analyzer.handle_po_analysis(session, config, brief=brief)

    # Two suggestions: grounded feature + bug (no evidence but bug → kept)
    assert len(added_items) == 2


@pytest.mark.asyncio
async def test_handle_po_analysis_drops_ungrounded_non_bugs():
    """Non-bug suggestions with no evidence_urls are filtered out."""
    from agent import po_analyzer

    brief = _make_brief()
    repo = _make_fake_repo()
    config = _make_fake_config()

    added_items: list = []
    session = _make_fake_session(
        latest_brief=brief,
        repo=repo,
        on_add=added_items.append,
    )

    po_agent = MagicMock()
    po_agent.run = AsyncMock(
        return_value=_make_agent_result(_po_output_one_ungrounded())
    )

    with (
        patch.object(po_analyzer, "create_agent", return_value=po_agent),
        patch.object(po_analyzer, "clone_repo", new=AsyncMock(return_value="/tmp/fake")),
        patch.object(po_analyzer, "remember_priority_suggestion", new=AsyncMock()),
        patch.object(po_analyzer, "publish", new=AsyncMock()),
    ):
        await po_analyzer.handle_po_analysis(session, config, brief=brief)

    # Only the grounded feature survives; ux_gap without evidence is dropped
    assert len(added_items) == 1
    assert added_items[0].title == "Add voice"


@pytest.mark.asyncio
async def test_handle_po_analysis_stamps_brief_id():
    """Persisted suggestions carry brief_id from the provided brief."""
    from agent import po_analyzer

    brief = _make_brief()
    brief.id = 42
    repo = _make_fake_repo()
    config = _make_fake_config()

    added_items: list = []
    session = _make_fake_session(
        latest_brief=brief,
        repo=repo,
        on_add=added_items.append,
    )

    po_agent = MagicMock()
    po_agent.run = AsyncMock(
        return_value=_make_agent_result(_po_output_two_grounded())
    )

    with (
        patch.object(po_analyzer, "create_agent", return_value=po_agent),
        patch.object(po_analyzer, "clone_repo", new=AsyncMock(return_value="/tmp/fake")),
        patch.object(po_analyzer, "remember_priority_suggestion", new=AsyncMock()),
        patch.object(po_analyzer, "publish", new=AsyncMock()),
    ):
        await po_analyzer.handle_po_analysis(session, config, brief=brief)

    assert all(item.brief_id == 42 for item in added_items)


# ---------------------------------------------------------------------------
# Full-chain scenario tests (researcher → _ensure_brief → handle_po_analysis)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_brief_triggers_researcher_then_po():
    """Stale brief → researcher runs → PO runs with new brief."""
    from agent import po_analyzer

    researcher_called = {"count": 0}
    po_called = {"count": 0}

    new_brief = _make_brief(age_days=0)
    stale_brief = _make_brief(age_days=10)
    repo = _make_fake_repo()
    config = _make_fake_config(market_brief_max_age_days=7)

    added_items: list = []
    session = _make_fake_session(
        latest_brief=stale_brief,
        repo=repo,
        on_add=added_items.append,
    )

    async def fake_run_market_research(*args, **kwargs):
        researcher_called["count"] += 1
        return new_brief

    po_agent = MagicMock()
    po_agent.run = AsyncMock(
        return_value=_make_agent_result(_po_output_two_grounded())
    )

    def fake_create_agent(*args, **kwargs):
        po_called["count"] += 1
        return po_agent

    with (
        patch("agent.po_analyzer.run_market_research", fake_run_market_research),
        patch.object(po_analyzer, "create_agent", fake_create_agent),
        patch.object(po_analyzer, "clone_repo", new=AsyncMock(return_value="/tmp/fake")),
        patch.object(po_analyzer, "remember_priority_suggestion", new=AsyncMock()),
        patch.object(po_analyzer, "publish", new=AsyncMock()),
    ):
        brief = await po_analyzer._ensure_brief(session, config)
        assert brief is new_brief
        await po_analyzer.handle_po_analysis(session, config, brief=brief)

    assert researcher_called["count"] == 1
    assert po_called["count"] == 1
    assert len(added_items) == 2  # feature with evidence + bug


@pytest.mark.asyncio
async def test_fresh_brief_skips_researcher():
    """Fresh brief → researcher NOT called, PO called."""
    from agent import po_analyzer

    researcher_called = {"count": 0}
    po_called = {"count": 0}

    fresh_brief = _make_brief(age_days=2)
    repo = _make_fake_repo()
    config = _make_fake_config(market_brief_max_age_days=7)

    added_items: list = []
    session = _make_fake_session(
        latest_brief=fresh_brief,
        repo=repo,
        on_add=added_items.append,
    )

    async def fake_run_market_research(*args, **kwargs):
        researcher_called["count"] += 1
        return None

    po_agent = MagicMock()
    po_agent.run = AsyncMock(
        return_value=_make_agent_result(_po_output_two_grounded())
    )

    def fake_create_agent(*args, **kwargs):
        po_called["count"] += 1
        return po_agent

    with (
        patch("agent.po_analyzer.run_market_research", fake_run_market_research),
        patch.object(po_analyzer, "create_agent", fake_create_agent),
        patch.object(po_analyzer, "clone_repo", new=AsyncMock(return_value="/tmp/fake")),
        patch.object(po_analyzer, "remember_priority_suggestion", new=AsyncMock()),
        patch.object(po_analyzer, "publish", new=AsyncMock()),
    ):
        brief = await po_analyzer._ensure_brief(session, config)
        assert brief is fresh_brief
        await po_analyzer.handle_po_analysis(session, config, brief=brief)

    assert researcher_called["count"] == 0
    assert po_called["count"] == 1


@pytest.mark.asyncio
async def test_researcher_failure_with_prior_brief_uses_prior():
    """Researcher fails, stale prior exists → PO runs with stale prior."""
    from agent import po_analyzer

    researcher_called = {"count": 0}
    po_called = {"count": 0}

    stale_brief = _make_brief(age_days=10)
    repo = _make_fake_repo()
    config = _make_fake_config(market_brief_max_age_days=7)

    added_items: list = []
    session = _make_fake_session(
        latest_brief=stale_brief,
        repo=repo,
        on_add=added_items.append,
    )

    async def fake_run_market_research(*args, **kwargs):
        researcher_called["count"] += 1
        return None  # researcher fails

    po_agent = MagicMock()
    po_agent.run = AsyncMock(
        return_value=_make_agent_result(_po_output_two_grounded())
    )

    def fake_create_agent(*args, **kwargs):
        po_called["count"] += 1
        return po_agent

    with (
        patch("agent.po_analyzer.run_market_research", fake_run_market_research),
        patch.object(po_analyzer, "create_agent", fake_create_agent),
        patch.object(po_analyzer, "clone_repo", new=AsyncMock(return_value="/tmp/fake")),
        patch.object(po_analyzer, "remember_priority_suggestion", new=AsyncMock()),
        patch.object(po_analyzer, "publish", new=AsyncMock()),
    ):
        brief = await po_analyzer._ensure_brief(session, config)
        assert brief is stale_brief  # fell back to stale
        await po_analyzer.handle_po_analysis(session, config, brief=brief)

    assert researcher_called["count"] == 1
    assert po_called["count"] == 1  # PO still ran with stale brief


@pytest.mark.asyncio
async def test_researcher_failure_no_prior_skips_cycle():
    """Researcher fails, no prior brief → _ensure_brief returns None → PO never called."""
    from agent import po_analyzer

    researcher_called = {"count": 0}
    po_called = {"count": 0}

    repo = _make_fake_repo()
    config = _make_fake_config(market_brief_max_age_days=7)

    session = _make_fake_session(latest_brief=None, repo=repo)

    async def fake_run_market_research(*args, **kwargs):
        researcher_called["count"] += 1
        return None  # researcher fails

    po_agent = MagicMock()
    po_agent.run = AsyncMock(return_value=_make_agent_result(_po_output_two_grounded()))

    def fake_create_agent(*args, **kwargs):
        po_called["count"] += 1
        return po_agent

    with (
        patch("agent.po_analyzer.run_market_research", fake_run_market_research),
        patch.object(po_analyzer, "create_agent", fake_create_agent),
        patch.object(po_analyzer, "clone_repo", new=AsyncMock(return_value="/tmp/fake")),
        patch.object(po_analyzer, "remember_priority_suggestion", new=AsyncMock()),
        patch.object(po_analyzer, "publish", new=AsyncMock()),
    ):
        brief = await po_analyzer._ensure_brief(session, config)
        # None → caller should skip PO; verify guard
        if brief is None:
            pass  # correctly skip PO
        else:
            await po_analyzer.handle_po_analysis(session, config, brief=brief)

    assert researcher_called["count"] == 1
    assert po_called["count"] == 0  # PO never called
    assert brief is None
