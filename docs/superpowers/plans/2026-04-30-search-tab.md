# Search Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a multi-turn Search tab in `web-next` backed by an agent runtime that searches the web (Brave), fetches URLs, and recalls/remembers facts in the shared team-memory graph. Streams sources, memory hits, and answer tokens as NDJSON.

**Architecture:** Reuse `agent/loop.py` (`AgentLoop`) with a curated tool subset. New tools live in `agent/tools/`. A thin `agent/search_loop.py` wraps `AgentLoop` to translate its `on_tool_call` / `on_thinking` callbacks plus per-tool event sinks into NDJSON event lines streamed by a new FastAPI router (`orchestrator/search.py`). Sessions and messages persist in two new SQLAlchemy tables. Frontend adds a `/search` route with a left-rail session list, chat pane, markdown answer rendering (via `react-markdown`), and a `useSearchStream` hook that parses NDJSON.

**Tech Stack:** FastAPI (existing), SQLAlchemy async + Alembic (existing), `requests` + `beautifulsoup4` + `html2text` (new Python deps), `team_memory.graph.GraphEngine` (existing), Next.js 14 App Router (existing), `react-markdown` (new web-next dep).

**Spec:** `docs/superpowers/specs/2026-04-30-search-tab-design.md`

---

## File map

### Created
- `migrations/versions/019_add_search_sessions.py` — DB migration.
- `agent/tools/web_search.py` — Brave search tool with `source` event sink.
- `agent/tools/fetch_url.py` — Fetch & clean a URL.
- `agent/tools/recall_memory.py` — `GraphEngine.recall` wrapper with `memory_hit` event sink.
- `agent/tools/remember_memory.py` — `GraphEngine.remember` wrapper, tightly scoped.
- `agent/search_loop.py` — `run_search_turn(...)` async generator yielding NDJSON event dicts.
- `agent/search_title.py` — One-shot LLM call generating a session title.
- `orchestrator/search.py` — FastAPI router: sessions CRUD + streaming messages.
- `tests/test_web_search_tool.py`
- `tests/test_fetch_url_tool.py`
- `tests/test_recall_memory_tool.py`
- `tests/test_remember_memory_tool.py`
- `tests/test_search_loop.py`
- `tests/test_search_endpoint.py`
- `web-next/app/(app)/search/page.tsx` — Tab entry: session list + chat pane.
- `web-next/components/search/session-list.tsx`
- `web-next/components/search/chat-pane.tsx`
- `web-next/components/search/message-bubble.tsx`
- `web-next/components/search/source-list.tsx`
- `web-next/components/search/memory-hits.tsx`
- `web-next/components/search/composer.tsx` — Input box + send/stop buttons.
- `web-next/hooks/useSearchStream.ts`
- `web-next/lib/search.ts` — Typed REST helpers and event types.
- `web-next/__tests__/useSearchStream.test.ts`

### Modified
- `shared/models.py` — Add `SearchSession`, `SearchMessage` ORM models.
- `shared/config.py` — Add `brave_api_key: str = ""`.
- `agent/tools/base.py` — Extend `ToolContext` with optional `event_sink`.
- `requirements.txt` (or `pyproject.toml`) — Add `beautifulsoup4`, `html2text`, `lxml`.
- `run.py` — Include the new search router under `/api`.
- `web-next/package.json` — Add `react-markdown`, `remark-gfm`.
- `web-next/components/sidebar/sidebar.tsx` — Add Search tab entry.

---

## Task 1: Add DB models and migration

**Files:**
- Modify: `shared/models.py`
- Create: `migrations/versions/019_add_search_sessions.py`
- Test: (verified via `alembic upgrade head` smoke + downstream tasks)

- [ ] **Step 1: Add ORM models to `shared/models.py`**

Append below the existing `User` class:

```python
class SearchSession(Base):
    """A multi-turn search/research conversation owned by a user."""
    __tablename__ = "search_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(512), nullable=False, default="New search")
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user = relationship("User")


class SearchMessage(Base):
    """A single turn in a SearchSession.

    For role='user': content is the raw user text, tool_events is empty.
    For role='assistant': content is the final markdown answer; tool_events
    is a list of {tool, args, result_summary, ts} captured during the turn,
    plus 'sources' and 'memory_hits' arrays.
    """
    __tablename__ = "search_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("search_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role = Column(String(16), nullable=False)  # "user" | "assistant"
    content = Column(Text, nullable=False, default="")
    tool_events = Column(JSONB, nullable=False, default=list)
    truncated = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    session = relationship("SearchSession")
```

- [ ] **Step 2: Create the alembic migration**

Create `migrations/versions/019_add_search_sessions.py`:

```python
"""Add search_sessions and search_messages tables.

Revision ID: 019
Revises: 018
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS search_sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title VARCHAR(512) NOT NULL DEFAULT 'New search',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_search_sessions_user_id ON search_sessions(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_search_sessions_updated_at ON search_sessions(updated_at DESC)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS search_messages (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            tool_events JSONB NOT NULL DEFAULT '[]'::jsonb,
            truncated BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_search_messages_session_id ON search_messages(session_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS search_messages")
    op.execute("DROP TABLE IF EXISTS search_sessions")
```

- [ ] **Step 3: Run the migration locally and verify**

Run:

```bash
docker compose exec auto-agent alembic upgrade head
```

Expected: ends with `Running upgrade 018 -> 019`. Verify tables exist:

```bash
docker compose exec auto-agent psql "$DATABASE_URL" -c "\dt search_*"
```

Expected output lists `search_sessions` and `search_messages`.

- [ ] **Step 4: Commit**

```bash
git add shared/models.py migrations/versions/019_add_search_sessions.py
git commit -m "feat(search): add search_sessions and search_messages tables"
```

---

## Task 2: Add config and Python dependencies

**Files:**
- Modify: `shared/config.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add `brave_api_key` to `Settings`**

In `shared/config.py`, add after `anthropic_api_key`:

```python
    # Search tab — Brave Search API key. /search endpoints return 503 if unset.
    brave_api_key: str = ""
```

- [ ] **Step 2: Add dependencies**

Append to `requirements.txt`:

```
beautifulsoup4>=4.12
html2text>=2024.2.26
lxml>=5.2
```

(If `pyproject.toml` is the source of truth instead, add the same three to its `dependencies` list. Check `pyproject.toml` first.)

- [ ] **Step 3: Install and verify imports**

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python3 -c "import bs4, html2text, lxml; print('ok')"
```

Expected output: `ok`.

- [ ] **Step 4: Commit**

```bash
git add shared/config.py requirements.txt
git commit -m "feat(search): add brave_api_key setting and HTML parsing deps"
```

---

## Task 3: Extend `ToolContext` with an event sink

**Files:**
- Modify: `agent/tools/base.py`
- Test: `tests/test_tool_context.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tool_context.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_tool_context.py -v
```

Expected: FAIL with `TypeError: ToolContext.__init__() got an unexpected keyword argument 'event_sink'`.

- [ ] **Step 3: Update `ToolContext`**

Edit `agent/tools/base.py`:

```python
from collections.abc import Awaitable, Callable

@dataclass
class ToolContext:
    """Execution context passed to every tool."""

    workspace: str
    readonly: bool = False
    # Optional async sink for tools that emit progress events to a streaming
    # caller (e.g. web_search emits 'source' events as Brave results arrive).
    event_sink: Callable[[dict], Awaitable[None]] | None = None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_tool_context.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run the full test suite to confirm no regression**

```bash
.venv/bin/python3 -m pytest tests/ -q
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add agent/tools/base.py tests/test_tool_context.py
git commit -m "feat(tools): add optional event_sink to ToolContext"
```

---

## Task 4: Implement `web_search` tool (Brave)

**Files:**
- Create: `agent/tools/web_search.py`
- Test: `tests/test_web_search_tool.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_web_search_tool.py`:

```python
import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.web_search import WebSearchTool


_BRAVE_RESPONSE = {
    "web": {
        "results": [
            {
                "url": "https://example.com/a",
                "title": "Example A",
                "description": "A description.",
            },
            {
                "url": "https://example.com/b",
                "title": "Example B",
                "description": "B description.",
            },
        ]
    }
}


@pytest.mark.asyncio
async def test_web_search_returns_results_and_emits_source_events():
    received: list[dict] = []

    async def sink(event: dict) -> None:
        received.append(event)

    ctx = ToolContext(workspace="/tmp", event_sink=sink)
    tool = WebSearchTool(api_key="fake")

    with patch("agent.tools.web_search._brave_get", new=AsyncMock(return_value=_BRAVE_RESPONSE)):
        result = await tool.execute({"query": "alpha", "num_results": 2}, ctx)

    assert not result.is_error
    payload = json.loads(result.output)
    assert len(payload["results"]) == 2
    assert payload["results"][0]["url"] == "https://example.com/a"
    assert payload["results"][0]["title"] == "Example A"
    # 2 source events, one per result
    assert [e["type"] for e in received] == ["source", "source"]
    assert received[0]["url"] == "https://example.com/a"
    assert received[0]["query"] == "alpha"


@pytest.mark.asyncio
async def test_web_search_missing_api_key_returns_error():
    ctx = ToolContext(workspace="/tmp")
    tool = WebSearchTool(api_key="")
    result = await tool.execute({"query": "x"}, ctx)
    assert result.is_error
    assert "BRAVE_API_KEY" in result.output


@pytest.mark.asyncio
async def test_web_search_handles_brave_failure():
    ctx = ToolContext(workspace="/tmp")
    tool = WebSearchTool(api_key="fake")
    with patch("agent.tools.web_search._brave_get", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await tool.execute({"query": "x"}, ctx)
    assert result.is_error
    assert "boom" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_web_search_tool.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the tool**

Create `agent/tools/web_search.py`:

```python
"""Brave Search API tool. Emits 'source' events as results arrive."""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from agent.tools.base import Tool, ToolContext, ToolResult

logger = structlog.get_logger()

_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


async def _brave_get(query: str, api_key: str, count: int) -> dict[str, Any]:
    """Call Brave Search and return the parsed JSON. Raises on HTTP error."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            _BRAVE_URL,
            params={"q": query, "count": count},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        resp.raise_for_status()
        return resp.json()


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web with Brave Search. Returns a list of results with "
        "url, title, and a short description (Brave's snippet). Use for "
        "current information beyond your training data."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query.",
            },
            "num_results": {
                "type": "integer",
                "description": "How many results to return (1-10). Default 6.",
                "default": 6,
            },
        },
        "required": ["query"],
    }
    is_readonly = True

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        query = (arguments.get("query") or "").strip()
        if not query:
            return ToolResult(output="Error: 'query' is required.", is_error=True)

        if not self._api_key:
            return ToolResult(
                output="Error: BRAVE_API_KEY is not configured on the server.",
                is_error=True,
            )

        count = max(1, min(10, int(arguments.get("num_results") or 6)))

        try:
            data = await _brave_get(query, self._api_key, count)
        except Exception as e:
            logger.warning("web_search_failed", error=str(e), query=query)
            return ToolResult(output=f"Error calling Brave Search: {e}", is_error=True)

        web = (data or {}).get("web") or {}
        raw_results = web.get("results") or []

        results: list[dict[str, str]] = []
        for r in raw_results[:count]:
            url = r.get("url") or ""
            if not url:
                continue
            item = {
                "url": url,
                "title": r.get("title") or url,
                "description": r.get("description") or "",
            }
            results.append(item)
            if context.event_sink is not None:
                try:
                    await context.event_sink({
                        "type": "source",
                        "url": item["url"],
                        "title": item["title"],
                        "summary": item["description"],
                        "query": query,
                    })
                except Exception:
                    pass

        return ToolResult(
            output=json.dumps({"query": query, "results": results}, ensure_ascii=False),
            token_estimate=sum(len(r["description"]) for r in results) // 4,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_web_search_tool.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/web_search.py tests/test_web_search_tool.py
git commit -m "feat(search): add web_search tool backed by Brave"
```

---

## Task 5: Implement `fetch_url` tool

**Files:**
- Create: `agent/tools/fetch_url.py`
- Test: `tests/test_fetch_url_tool.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fetch_url_tool.py`:

```python
import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.fetch_url import FetchUrlTool

_HTML = """
<html><head><title>Hello World</title></head>
<body><h1>Hi</h1><p>Some <b>bold</b> text and a <a href="x">link</a>.</p>
<script>console.log('nope')</script>
</body></html>
"""


@pytest.mark.asyncio
async def test_fetch_url_returns_title_and_markdown():
    ctx = ToolContext(workspace="/tmp")
    tool = FetchUrlTool()
    with patch("agent.tools.fetch_url._http_get", new=AsyncMock(return_value=_HTML)):
        result = await tool.execute({"url": "https://example.com"}, ctx)
    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["title"] == "Hello World"
    assert "Hi" in payload["content"]
    assert "console.log" not in payload["content"]


@pytest.mark.asyncio
async def test_fetch_url_truncates_long_content():
    ctx = ToolContext(workspace="/tmp")
    tool = FetchUrlTool(max_chars=200)
    big_html = "<html><body>" + ("x" * 5000) + "</body></html>"
    with patch("agent.tools.fetch_url._http_get", new=AsyncMock(return_value=big_html)):
        result = await tool.execute({"url": "https://example.com"}, ctx)
    payload = json.loads(result.output)
    assert len(payload["content"]) <= 220  # 200 plus the trailing notice
    assert "truncated" in payload["content"].lower()


@pytest.mark.asyncio
async def test_fetch_url_handles_http_error():
    ctx = ToolContext(workspace="/tmp")
    tool = FetchUrlTool()
    with patch("agent.tools.fetch_url._http_get", new=AsyncMock(side_effect=RuntimeError("404"))):
        result = await tool.execute({"url": "https://example.com"}, ctx)
    assert result.is_error
    assert "404" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_fetch_url_tool.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the tool**

Create `agent/tools/fetch_url.py`:

```python
"""Fetch a URL and return its main text content as markdown."""

from __future__ import annotations

import json
from typing import Any

import html2text
import httpx
import structlog
from bs4 import BeautifulSoup

from agent.tools.base import Tool, ToolContext, ToolResult

logger = structlog.get_logger()

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


async def _http_get(url: str, timeout: float = 15.0) -> str:
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = (
        "Fetch a URL and return its main text content as markdown. "
        "Use when web_search snippets are insufficient and you need the "
        "full page content to answer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL to fetch."}
        },
        "required": ["url"],
    }
    is_readonly = True

    def __init__(self, max_chars: int = 32_000) -> None:
        self._max_chars = max_chars

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        url = (arguments.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(output="Error: 'url' must be http(s)://...", is_error=True)

        try:
            html = await _http_get(url)
        except Exception as e:
            logger.warning("fetch_url_failed", error=str(e), url=url)
            return ToolResult(output=f"Error fetching URL: {e}", is_error=True)

        soup = BeautifulSoup(html, "lxml")
        title_tag = soup.find("title")
        title = (title_tag.get_text(strip=True) if title_tag else "") or url

        for tag in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav"]):
            tag.decompose()

        converter = html2text.HTML2Text()
        converter.ignore_images = True
        converter.ignore_links = False
        converter.body_width = 0
        markdown = converter.handle(str(soup)).strip()

        if len(markdown) > self._max_chars:
            markdown = markdown[: self._max_chars] + "\n\n[content truncated]"

        return ToolResult(
            output=json.dumps({"url": url, "title": title, "content": markdown}, ensure_ascii=False),
            token_estimate=len(markdown) // 4,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_fetch_url_tool.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/fetch_url.py tests/test_fetch_url_tool.py
git commit -m "feat(search): add fetch_url tool"
```

---

## Task 6: Implement `recall_memory` tool

**Files:**
- Create: `agent/tools/recall_memory.py`
- Test: `tests/test_recall_memory_tool.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_recall_memory_tool.py`:

```python
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.recall_memory import RecallMemoryTool


_RECALL_RESULT = {
    "ambiguous": False,
    "matches": [
        {
            "entity": {"id": "e1", "name": "Auto-Agent", "type": "project", "tags": []},
            "facts": [
                {"id": "f1", "content": "Personal project.", "kind": "decision",
                 "valid_from": None, "valid_until": None, "source": None},
            ],
            "relevance": 1.0,
        }
    ],
}


class _FakeSessionCtx:
    def __init__(self, session): self._session = session
    async def __aenter__(self): return self._session
    async def __aexit__(self, *a): return None


@pytest.mark.asyncio
async def test_recall_memory_returns_matches_and_emits_events():
    received: list[dict] = []

    async def sink(event: dict) -> None:
        received.append(event)

    ctx = ToolContext(workspace="/tmp", event_sink=sink)
    tool = RecallMemoryTool()

    fake_session = MagicMock()
    fake_engine = MagicMock()
    fake_engine.recall = AsyncMock(return_value=_RECALL_RESULT)

    with patch("agent.tools.recall_memory.team_memory_session", lambda: _FakeSessionCtx(fake_session)), \
         patch("agent.tools.recall_memory.GraphEngine", return_value=fake_engine):
        result = await tool.execute({"query": "auto-agent"}, ctx)

    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["matches"][0]["entity"]["name"] == "Auto-Agent"
    assert [e["type"] for e in received] == ["memory_hit"]
    assert received[0]["entity"]["name"] == "Auto-Agent"
    assert received[0]["facts"][0]["content"] == "Personal project."


@pytest.mark.asyncio
async def test_recall_memory_session_unavailable():
    ctx = ToolContext(workspace="/tmp")
    tool = RecallMemoryTool()
    with patch("agent.tools.recall_memory.team_memory_session", None):
        result = await tool.execute({"query": "x"}, ctx)
    assert result.is_error
    assert "team-memory" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_recall_memory_tool.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the tool**

Create `agent/tools/recall_memory.py`:

```python
"""Recall facts from the shared team-memory graph."""

from __future__ import annotations

import json
from typing import Any

import structlog
from team_memory.graph import GraphEngine

from agent.tools.base import Tool, ToolContext, ToolResult
from shared.database import team_memory_session

logger = structlog.get_logger()


class RecallMemoryTool(Tool):
    name = "recall_memory"
    description = (
        "Look up facts in the shared team-memory knowledge graph by entity "
        "name or topic. Use this BEFORE searching the web when the question "
        "is about the team, the project, or anything previously remembered."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Entity name or topic to recall.",
            },
        },
        "required": ["query"],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        query = (arguments.get("query") or "").strip()
        if not query:
            return ToolResult(output="Error: 'query' is required.", is_error=True)

        if team_memory_session is None:
            return ToolResult(
                output="Error: team-memory is not configured on this server.",
                is_error=True,
            )

        try:
            async with team_memory_session() as session:
                engine = GraphEngine(session)
                result = await engine.recall(query=query)
        except Exception as e:
            logger.warning("recall_memory_failed", error=str(e), query=query)
            return ToolResult(output=f"Error recalling memory: {e}", is_error=True)

        if context.event_sink is not None:
            for match in result.get("matches") or []:
                try:
                    await context.event_sink({
                        "type": "memory_hit",
                        "entity": match["entity"],
                        "facts": match["facts"],
                    })
                except Exception:
                    pass

        return ToolResult(output=json.dumps(result, ensure_ascii=False))
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_recall_memory_tool.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/recall_memory.py tests/test_recall_memory_tool.py
git commit -m "feat(search): add recall_memory tool"
```

---

## Task 7: Implement `remember_memory` tool

**Files:**
- Create: `agent/tools/remember_memory.py`
- Test: `tests/test_remember_memory_tool.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_remember_memory_tool.py`:

```python
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.remember_memory import RememberMemoryTool


class _FakeSessionCtx:
    def __init__(self, session): self._session = session
    async def __aenter__(self): return self._session
    async def __aexit__(self, *a): return None


@pytest.mark.asyncio
async def test_remember_memory_writes_fact():
    ctx = ToolContext(workspace="/tmp")
    tool = RememberMemoryTool(author="alan@ergodic.ai")

    fake_session = MagicMock()
    fake_session.commit = AsyncMock()
    fake_engine = MagicMock()
    fake_engine.remember = AsyncMock(return_value={
        "entity_id": "e1", "fact_id": "f1", "created_entity": True,
    })

    with patch("agent.tools.remember_memory.team_memory_session", lambda: _FakeSessionCtx(fake_session)), \
         patch("agent.tools.remember_memory.GraphEngine", return_value=fake_engine):
        result = await tool.execute({
            "entity_name": "Alan",
            "entity_type": "person",
            "fact": "Prefers terse responses.",
            "kind": "preference",
        }, ctx)

    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["fact_id"] == "f1"
    fake_engine.remember.assert_awaited_once()
    fake_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_remember_memory_session_unavailable():
    ctx = ToolContext(workspace="/tmp")
    tool = RememberMemoryTool()
    with patch("agent.tools.remember_memory.team_memory_session", None):
        result = await tool.execute({
            "entity_name": "Alan", "entity_type": "person",
            "fact": "x", "kind": "preference",
        }, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_remember_memory_validates_required_fields():
    ctx = ToolContext(workspace="/tmp")
    tool = RememberMemoryTool()
    result = await tool.execute({"entity_name": "Alan"}, ctx)
    assert result.is_error
    assert "fact" in result.output.lower() or "required" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_remember_memory_tool.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the tool**

Create `agent/tools/remember_memory.py`:

```python
"""Persist a fact to the shared team-memory graph.

Tightly scoped: the agent should ONLY call this for user-stated preferences
or durable personal/project facts the user has explicitly asked to remember.
Web research findings are explicitly out of scope.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from team_memory.graph import GraphEngine

from agent.tools.base import Tool, ToolContext, ToolResult
from shared.database import team_memory_session

logger = structlog.get_logger()


class RememberMemoryTool(Tool):
    name = "remember_memory"
    description = (
        "Save a fact to the shared team-memory graph. Use ONLY when:\n"
        "  (a) the user has explicitly asked you to remember something, OR\n"
        "  (b) the user has stated a durable preference or fact about "
        "themselves or the project that will be useful in future "
        "conversations.\n"
        "DO NOT save research findings, web search summaries, or anything "
        "you learned from web_search / fetch_url. Those belong in the "
        "current search session, not in team-memory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "entity_name": {
                "type": "string",
                "description": "Canonical name of the entity the fact is about (e.g. 'Alan', 'Auto-Agent').",
            },
            "entity_type": {
                "type": "string",
                "description": "Entity type, e.g. 'person', 'project', 'team', 'system'.",
            },
            "fact": {
                "type": "string",
                "description": "The fact to remember, as a single concise sentence.",
            },
            "kind": {
                "type": "string",
                "description": "One of: preference, decision, status, note.",
                "enum": ["preference", "decision", "status", "note"],
            },
        },
        "required": ["entity_name", "entity_type", "fact", "kind"],
    }
    is_readonly = False

    def __init__(self, author: str | None = None) -> None:
        self._author = author

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        for field in ("entity_name", "entity_type", "fact", "kind"):
            if not arguments.get(field):
                return ToolResult(
                    output=f"Error: '{field}' is required.",
                    is_error=True,
                )

        if team_memory_session is None:
            return ToolResult(
                output="Error: team-memory is not configured on this server.",
                is_error=True,
            )

        try:
            async with team_memory_session() as session:
                engine = GraphEngine(session)
                result = await engine.remember(
                    content=arguments["fact"],
                    entity=arguments["entity_name"],
                    entity_type=arguments["entity_type"],
                    kind=arguments["kind"],
                    source="search-tab",
                    author=self._author,
                )
                await session.commit()
        except Exception as e:
            logger.warning("remember_memory_failed", error=str(e))
            return ToolResult(output=f"Error remembering fact: {e}", is_error=True)

        return ToolResult(output=json.dumps({"ok": True, **result}, ensure_ascii=False))
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_remember_memory_tool.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/remember_memory.py tests/test_remember_memory_tool.py
git commit -m "feat(search): add remember_memory tool"
```

---

## Task 8: Implement `search_loop.run_search_turn`

**Files:**
- Create: `agent/search_loop.py`
- Test: `tests/test_search_loop.py`

The wrapper instantiates the four search tools, configures `AgentLoop` with a search-specific system prompt, and turns its callbacks plus the per-tool event sink into a stream of NDJSON event dicts (yielded by an async generator).

- [ ] **Step 1: Write the failing test**

Create `tests/test_search_loop.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.search_loop import run_search_turn
from agent.llm.types import Message, TokenUsage


class _FakeAgentLoop:
    """Stub AgentLoop that drives the on_thinking / on_tool_call callbacks
    and then returns an AgentResult with a final answer."""

    def __init__(self, *args, **kwargs):
        self._on_thinking = kwargs.get("on_thinking")
        self._on_tool_call = kwargs.get("on_tool_call")

    async def run(self, prompt, system=None, resume=False):
        # Simulate one tool call + token streaming
        if self._on_tool_call:
            await self._on_tool_call("web_search", {"query": "alpha"}, "ok", 0)
        if self._on_thinking:
            await self._on_thinking("Hello", 0)
            await self._on_thinking(" world.", 0)
        from agent.loop import AgentResult
        return AgentResult(
            output="Hello world.",
            tool_calls_made=1,
            tokens_used=TokenUsage(),
            messages=[Message(role="assistant", content="Hello world.")],
        )


@pytest.mark.asyncio
async def test_run_search_turn_emits_expected_events():
    history = [
        {"role": "user", "content": "What is alpha?"},
    ]
    events: list[dict] = []
    with patch("agent.search_loop.AgentLoop", _FakeAgentLoop), \
         patch("agent.search_loop.get_provider", return_value=MagicMock(is_passthrough=False)):
        async for ev in run_search_turn(
            user_message="What is alpha?",
            history=history,
            brave_api_key="fake",
            author="alan",
        ):
            events.append(ev)

    types = [e["type"] for e in events]
    assert "tool_call_start" in types
    assert "text" in types
    assert types[-1] == "done"
    text_events = [e for e in events if e["type"] == "text"]
    assert "".join(e["delta"] for e in text_events) == "Hello world."


@pytest.mark.asyncio
async def test_run_search_turn_emits_error_on_exception():
    class _Boom:
        def __init__(self, *a, **kw): pass
        async def run(self, *a, **kw): raise RuntimeError("boom")

    events: list[dict] = []
    with patch("agent.search_loop.AgentLoop", _Boom), \
         patch("agent.search_loop.get_provider", return_value=MagicMock(is_passthrough=False)):
        async for ev in run_search_turn(
            user_message="x", history=[], brave_api_key="fake", author=None,
        ):
            events.append(ev)
    assert events[-1]["type"] == "error"
    assert "boom" in events[-1]["message"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_search_loop.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `agent/search_loop.py`**

Create `agent/search_loop.py`:

```python
"""Run one search-tab agent turn and yield NDJSON event dicts.

This is a thin shell around `agent.loop.AgentLoop`:

  * Builds a curated ToolRegistry: web_search, fetch_url, recall_memory,
    remember_memory.
  * Builds a search-specific system prompt that tells the agent to recall
    team-memory before searching the web, and to use remember_memory only
    for user-stated preferences (not research).
  * Translates the loop's on_tool_call / on_thinking callbacks plus tools'
    own event_sink emissions into NDJSON event dicts on a queue.
  * Yields events to the HTTP layer until the loop completes or errors.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import structlog

from agent.context import ContextManager
from agent.context.memory import query_relevant_memory
from agent.llm import get_provider
from agent.llm.types import Message
from agent.loop import AgentLoop
from agent.session import Session
from agent.tools.base import ToolContext, ToolRegistry
from agent.tools.fetch_url import FetchUrlTool
from agent.tools.recall_memory import RecallMemoryTool
from agent.tools.remember_memory import RememberMemoryTool
from agent.tools.web_search import WebSearchTool

logger = structlog.get_logger()

_SYSTEM_PROMPT = """You are a research assistant in the Auto-Agent search tab.

You have four tools:
  - recall_memory: look up the team-memory knowledge graph
  - web_search: Brave search for current web information
  - fetch_url: read the full text of a specific URL
  - remember_memory: save a fact to team-memory

Workflow for each user message:
1. If the question is about the user, the team, or this project, START with
   recall_memory. The answer may already be there.
2. If the question is about the wider world or current events, use web_search.
   Use multiple targeted queries rather than one broad query.
3. If a result's snippet looks promising but lacks detail, use fetch_url.
4. Synthesize a markdown answer with concise inline citations like
   [example.com](https://example.com).
5. Use remember_memory ONLY when the user has explicitly asked you to
   remember something, or has stated a durable preference about themselves
   or the project. Do NOT use it to save web research.

Be terse. Use bullet points and short paragraphs."""


def _build_tools(brave_api_key: str, author: str | None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(WebSearchTool(api_key=brave_api_key))
    registry.register(FetchUrlTool())
    registry.register(RecallMemoryTool())
    registry.register(RememberMemoryTool(author=author))
    return registry


def _history_to_messages(history: list[dict]) -> list[Message]:
    out: list[Message] = []
    for h in history:
        role = h.get("role")
        content = h.get("content") or ""
        if role in ("user", "assistant") and content:
            out.append(Message(role=role, content=content))
    return out


async def run_search_turn(
    *,
    user_message: str,
    history: list[dict],
    brave_api_key: str,
    author: str | None,
) -> AsyncIterator[dict]:
    """Run one search agent turn. Yields NDJSON event dicts.

    Event types emitted:
      * tool_call_start  {tool, args}
      * source           {url, title, summary, query}
      * memory_hit       {entity, facts}
      * text             {delta}
      * done             {answer}
      * error            {message}
    """

    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def event_sink(event: dict) -> None:
        await queue.put(event)

    async def on_tool_call(name: str, args: dict, _preview: str, _turn: int) -> None:
        await queue.put({"type": "tool_call_start", "tool": name, "args": args})

    async def on_thinking(text: str, _turn: int) -> None:
        if text:
            await queue.put({"type": "text", "delta": text})

    tools = _build_tools(brave_api_key, author)

    # Inject pre-recall as a system-prompt suffix to bias the agent.
    pre_recall = await query_relevant_memory(user_message)
    system_prompt = _SYSTEM_PROMPT + (("\n\n" + pre_recall) if pre_recall else "")

    provider = get_provider()
    context_manager = ContextManager(provider=provider, workspace=".")

    # Convert prior turns into the loop's message format. The loop will
    # append the new user_message itself.
    prior_messages = _history_to_messages(history)

    # Wrap the existing AgentLoop with our callbacks. We rely on AgentLoop's
    # own session-less mode (session=None) — we manage persistence in the
    # router rather than here.
    loop = AgentLoop(
        provider=provider,
        tools=tools,
        context_manager=context_manager,
        session=None,
        max_turns=12,
        workspace=".",
        on_tool_call=on_tool_call,
        on_thinking=on_thinking,
    )

    # Inject the event_sink into the ToolContext used by tools. AgentLoop
    # currently constructs ToolContext internally; expose it via a monkey
    # attribute the loop will read. (See agent/loop.py: ToolContext is built
    # at the top of _run_agentic.) We set it on each tool registry entry's
    # default context by wrapping execute.
    for tool in [tools.get(n) for n in tools.names()]:
        original = tool.execute

        async def patched(args, ctx, _orig=original):
            ctx.event_sink = event_sink
            return await _orig(args, ctx)

        tool.execute = patched  # type: ignore[assignment]

    async def runner() -> None:
        try:
            # Build a synthetic prior-message list by setting api_messages on
            # the loop's session-less path. Easiest: serialize history as a
            # single 'previous turns' system suffix.
            prior_summary = "\n\n".join(
                f"{m.role.upper()}: {m.content}" for m in prior_messages
            )
            full_prompt = (
                (f"Previous turns in this session:\n{prior_summary}\n\n---\n\n"
                 if prior_summary else "")
                + f"User: {user_message}"
            )
            result = await loop.run(prompt=full_prompt, system=system_prompt)
            await queue.put({"type": "done", "answer": result.output})
        except Exception as e:
            logger.warning("search_loop_failed", error=str(e))
            await queue.put({"type": "error", "message": str(e)})
        finally:
            await queue.put(None)  # sentinel

    task = asyncio.create_task(runner())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        if not task.done():
            task.cancel()
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_search_loop.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add agent/search_loop.py tests/test_search_loop.py
git commit -m "feat(search): add search_loop wrapper that yields NDJSON events"
```

---

## Task 9: Implement title generator

**Files:**
- Create: `agent/search_title.py`
- Test: extends `tests/test_search_loop.py` with a `test_generate_title` (mocked provider)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search_loop.py`:

```python
from unittest.mock import AsyncMock

from agent.llm.types import LLMResponse, Message as LMessage, TokenUsage
from agent.search_title import generate_title


@pytest.mark.asyncio
async def test_generate_title_returns_short_string():
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(return_value=LLMResponse(
        message=LMessage(role="assistant", content="Best CLI for git"),
        usage=TokenUsage(),
        stop_reason="end_turn",
    ))
    with patch("agent.search_title.get_provider", return_value=fake_provider):
        title = await generate_title("what's the best CLI tool for git?")
    assert title == "Best CLI for git"
    fake_provider.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_title_falls_back_on_error():
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("agent.search_title.get_provider", return_value=fake_provider):
        title = await generate_title("what's the best CLI tool for git?")
    assert title == "what's the best CLI tool for git?"[:80]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_search_loop.py::test_generate_title_returns_short_string -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `agent/search_title.py`**

Create `agent/search_title.py`:

```python
"""Generate a short title from the first user message in a search session."""

from __future__ import annotations

import structlog

from agent.llm import get_provider
from agent.llm.types import Message

logger = structlog.get_logger()

_SYSTEM = (
    "You generate short titles (2-6 words) for search sessions. "
    "Output the title only, no quotes, no punctuation at the end."
)


async def generate_title(first_message: str) -> str:
    """Generate a short title for a search session, or fall back to a slice
    of the user's message on any error."""
    fallback = (first_message or "").strip()[:80] or "New search"
    try:
        provider = get_provider()
        response = await provider.complete(
            messages=[Message(role="user", content=first_message)],
            system=_SYSTEM,
        )
        title = (response.message.content or "").strip().strip('"').strip("'")
        return title[:80] or fallback
    except Exception as e:
        logger.warning("generate_title_failed", error=str(e))
        return fallback
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_search_loop.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add agent/search_title.py tests/test_search_loop.py
git commit -m "feat(search): add session title generator"
```

---

## Task 10: Implement search router (sessions CRUD + streaming)

**Files:**
- Create: `orchestrator/search.py`
- Test: `tests/test_search_endpoint.py`
- Modify: `run.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_search_endpoint.py`:

```python
import json
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from fastapi import FastAPI
from sqlalchemy import select

from orchestrator.search import router as search_router
from shared.database import async_session
from shared.models import SearchMessage, SearchSession, User


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(search_router, prefix="/api")
    return app


async def _make_user(username="alan") -> User:
    async with async_session() as s:
        u = User(username=username, password_hash="x", display_name=username)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


@pytest.mark.asyncio
async def test_create_session_and_list(app, monkeypatch):
    user = await _make_user()
    monkeypatch.setattr(
        "orchestrator.search._current_user_id",
        lambda *a, **kw: user.id,
    )
    async with AsyncClient(app=app, base_url="http://t") as client:
        r = await client.post("/api/search/sessions", json={})
        assert r.status_code == 200
        sid = r.json()["id"]

        r = await client.get("/api/search/sessions")
        assert r.status_code == 200
        assert any(s["id"] == sid for s in r.json())


@pytest.mark.asyncio
async def test_send_message_streams_events_and_persists(app, monkeypatch):
    user = await _make_user(username="alan2")
    monkeypatch.setattr(
        "orchestrator.search._current_user_id",
        lambda *a, **kw: user.id,
    )

    async def fake_run_search_turn(**kwargs):
        yield {"type": "tool_call_start", "tool": "web_search", "args": {"query": "x"}}
        yield {"type": "source", "url": "https://x.com", "title": "X", "summary": "...", "query": "x"}
        yield {"type": "text", "delta": "Hello"}
        yield {"type": "done", "answer": "Hello"}

    with patch("orchestrator.search.run_search_turn", fake_run_search_turn):
        async with AsyncClient(app=app, base_url="http://t") as client:
            r = await client.post("/api/search/sessions", json={})
            sid = r.json()["id"]

            async with client.stream(
                "POST",
                f"/api/search/sessions/{sid}/messages",
                json={"content": "hello"},
            ) as resp:
                assert resp.status_code == 200
                lines: list[dict] = []
                async for line in resp.aiter_lines():
                    if line.strip():
                        lines.append(json.loads(line))

    types = [ev["type"] for ev in lines]
    assert "tool_call_start" in types
    assert "source" in types
    assert types[-1] == "done"

    async with async_session() as s:
        rows = (await s.execute(
            select(SearchMessage).where(SearchMessage.session_id == sid).order_by(SearchMessage.id)
        )).scalars().all()
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[1].content == "Hello"
    sources = [e for e in rows[1].tool_events if e.get("type") == "source"]
    assert sources and sources[0]["url"] == "https://x.com"


@pytest.mark.asyncio
async def test_send_message_503_when_brave_unset(app, monkeypatch):
    user = await _make_user(username="alan3")
    monkeypatch.setattr(
        "orchestrator.search._current_user_id",
        lambda *a, **kw: user.id,
    )
    monkeypatch.setattr("orchestrator.search.settings.brave_api_key", "")
    async with AsyncClient(app=app, base_url="http://t") as client:
        r = await client.post("/api/search/sessions", json={})
        sid = r.json()["id"]
        r = await client.post(
            f"/api/search/sessions/{sid}/messages",
            json={"content": "hello"},
        )
    assert r.status_code == 503
    assert "BRAVE_API_KEY" in r.json()["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_search_endpoint.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `orchestrator/search.py`**

Create `orchestrator/search.py`:

```python
"""Search tab API: sessions CRUD + streaming message endpoint."""

from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.search_loop import run_search_turn
from agent.search_title import generate_title
from orchestrator.auth import verify_token
from shared.config import settings
from shared.database import get_session
from shared.models import SearchMessage, SearchSession, User

router = APIRouter()
_COOKIE_NAME = "auto_agent_session"


def _verify_cookie_or_header(cookie: str | None, authorization: str | None) -> dict:
    if cookie:
        payload = verify_token(cookie)
        if payload:
            return payload
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            return payload
    raise HTTPException(status_code=401, detail="Not authenticated")


def _current_user_id(
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> int:
    return _verify_cookie_or_header(auto_agent_session, authorization)["user_id"]


# ---------- Schemas ----------


class CreateSessionRequest(BaseModel):
    pass


class SessionData(BaseModel):
    id: int
    title: str
    created_at: str
    updated_at: str


class MessageData(BaseModel):
    id: int
    role: str
    content: str
    tool_events: list
    truncated: bool
    created_at: str


class SessionDetail(SessionData):
    messages: list[MessageData]


class SendMessageRequest(BaseModel):
    content: str


# ---------- Sessions CRUD ----------


def _serialize_session(s: SearchSession) -> SessionData:
    return SessionData(
        id=s.id,
        title=s.title,
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


@router.post("/search/sessions", response_model=SessionData)
async def create_session(
    _req: CreateSessionRequest,
    user_id: int = Depends(_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> SessionData:
    s = SearchSession(user_id=user_id, title="New search")
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return _serialize_session(s)


@router.get("/search/sessions", response_model=list[SessionData])
async def list_sessions(
    user_id: int = Depends(_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> list[SessionData]:
    rows = (await session.execute(
        select(SearchSession)
        .where(SearchSession.user_id == user_id)
        .order_by(desc(SearchSession.updated_at))
    )).scalars().all()
    return [_serialize_session(r) for r in rows]


@router.get("/search/sessions/{session_id}", response_model=SessionDetail)
async def get_session_detail(
    session_id: int,
    user_id: int = Depends(_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> SessionDetail:
    row = (await session.execute(
        select(SearchSession).where(
            SearchSession.id == session_id,
            SearchSession.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    msgs = (await session.execute(
        select(SearchMessage)
        .where(SearchMessage.session_id == session_id)
        .order_by(SearchMessage.id)
    )).scalars().all()

    return SessionDetail(
        **_serialize_session(row).model_dump(),
        messages=[
            MessageData(
                id=m.id,
                role=m.role,
                content=m.content,
                tool_events=list(m.tool_events or []),
                truncated=m.truncated,
                created_at=m.created_at.isoformat(),
            )
            for m in msgs
        ],
    )


@router.delete("/search/sessions/{session_id}")
async def delete_session(
    session_id: int,
    user_id: int = Depends(_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = (await session.execute(
        select(SearchSession).where(
            SearchSession.id == session_id,
            SearchSession.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


# ---------- Streaming ----------


async def _load_history(session: AsyncSession, session_id: int) -> list[dict]:
    rows = (await session.execute(
        select(SearchMessage)
        .where(SearchMessage.session_id == session_id)
        .order_by(SearchMessage.id)
    )).scalars().all()
    return [{"role": r.role, "content": r.content} for r in rows]


@router.post("/search/sessions/{session_id}/messages")
async def send_message(
    session_id: int,
    req: SendMessageRequest,
    user_id: int = Depends(_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    if not settings.brave_api_key:
        raise HTTPException(
            status_code=503,
            detail="Search is not configured. Set BRAVE_API_KEY.",
        )

    # Authorize + load history
    sess = (await session.execute(
        select(SearchSession).where(
            SearchSession.id == session_id,
            SearchSession.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    user_row = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one()
    author = user_row.username

    # Persist the user message before we start streaming
    user_msg = SearchMessage(session_id=session_id, role="user", content=req.content)
    session.add(user_msg)
    await session.commit()

    history = await _load_history(session, session_id)
    is_first_user_message = sum(1 for h in history if h["role"] == "user") == 1

    async def stream() -> AsyncIterator[bytes]:
        events_captured: list[dict] = []
        answer = ""
        truncated = False
        try:
            async for ev in run_search_turn(
                user_message=req.content,
                history=history[:-1],  # exclude the message we just appended
                brave_api_key=settings.brave_api_key,
                author=author,
            ):
                events_captured.append(ev)
                if ev["type"] == "text":
                    answer += ev.get("delta", "")
                yield (json.dumps(ev) + "\n").encode("utf-8")
        except Exception as e:
            truncated = True
            yield (json.dumps({"type": "error", "message": str(e)}) + "\n").encode("utf-8")
        finally:
            # Persist assistant message + bump session updated_at + maybe set title
            from shared.database import async_session as _async_session
            async with _async_session() as s2:
                msg = SearchMessage(
                    session_id=session_id,
                    role="assistant",
                    content=answer,
                    tool_events=events_captured,
                    truncated=truncated,
                )
                s2.add(msg)
                # Bump updated_at
                target = (await s2.execute(
                    select(SearchSession).where(SearchSession.id == session_id)
                )).scalar_one()
                target.updated_at = msg.created_at or target.updated_at
                if is_first_user_message and target.title == "New search":
                    target.title = await generate_title(req.content)
                await s2.commit()

    return StreamingResponse(stream(), media_type="application/x-ndjson")
```

- [ ] **Step 4: Wire the router into `run.py`**

Edit `run.py`. After the existing router includes (around line 147), add:

```python
from orchestrator.search import router as search_router
app.include_router(search_router, prefix="/api")
```

- [ ] **Step 5: Run the tests**

```bash
.venv/bin/python3 -m pytest tests/test_search_endpoint.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Run the full backend suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
ruff check .
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/search.py tests/test_search_endpoint.py run.py
git commit -m "feat(search): add session CRUD + streaming messages endpoint"
```

---

## Task 11: Add `react-markdown` and the Search sidebar tab

**Files:**
- Modify: `web-next/package.json`
- Modify: `web-next/components/sidebar/sidebar.tsx`

- [ ] **Step 1: Add the dependency**

```bash
cd web-next && npm install react-markdown@9 remark-gfm@4 && cd ..
```

Expected: package.json updated with `react-markdown` and `remark-gfm` under `dependencies`.

- [ ] **Step 2: Add the tab entry**

Edit `web-next/components/sidebar/sidebar.tsx`. Update the `tabs` array:

```typescript
const tabs = [
  { href: '/tasks', label: 'Tasks' },
  { href: '/freeform', label: 'Freeform' },
  { href: '/memory', label: 'Memory' },
  { href: '/search', label: 'Search' },
];
```

- [ ] **Step 3: Verify the build still passes**

```bash
cd web-next && npm run typecheck && cd ..
```

Expected: no type errors.

- [ ] **Step 4: Commit**

```bash
git add web-next/package.json web-next/package-lock.json web-next/components/sidebar/sidebar.tsx
git commit -m "feat(web-next): add react-markdown dep and Search sidebar tab"
```

---

## Task 12: Add typed REST helpers and event types

**Files:**
- Create: `web-next/lib/search.ts`

- [ ] **Step 1: Create the helper module**

Create `web-next/lib/search.ts`:

```typescript
import { api } from './api';

export type Source = {
  url: string;
  title: string;
  summary: string;
  query: string;
};

export type MemoryHit = {
  entity: { id: string; name: string; type: string; tags: string[] };
  facts: Array<{ id: string; content: string; kind: string; source: string | null }>;
};

export type ToolCallStart = {
  type: 'tool_call_start';
  tool: string;
  args: Record<string, unknown>;
};

export type SearchEvent =
  | (ToolCallStart)
  | ({ type: 'source' } & Source)
  | ({ type: 'memory_hit' } & MemoryHit)
  | { type: 'text'; delta: string }
  | { type: 'done'; answer: string }
  | { type: 'error'; message: string };

export type SearchSession = {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
};

export type SearchMessage = {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  tool_events: SearchEvent[];
  truncated: boolean;
  created_at: string;
};

export type SearchSessionDetail = SearchSession & { messages: SearchMessage[] };

export const createSession = () =>
  api<SearchSession>('/api/search/sessions', { method: 'POST', body: '{}' });

export const listSessions = () =>
  api<SearchSession[]>('/api/search/sessions');

export const getSession = (id: number) =>
  api<SearchSessionDetail>(`/api/search/sessions/${id}`);

export const deleteSession = (id: number) =>
  api<{ ok: true }>(`/api/search/sessions/${id}`, { method: 'DELETE' });
```

- [ ] **Step 2: Typecheck**

```bash
cd web-next && npm run typecheck && cd ..
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add web-next/lib/search.ts
git commit -m "feat(web-next): add search REST helpers and event types"
```

---

## Task 13: Implement `useSearchStream` hook

**Files:**
- Create: `web-next/hooks/useSearchStream.ts`
- Test: `web-next/__tests__/useSearchStream.test.ts`

- [ ] **Step 1: Write the failing test**

Create `web-next/__tests__/useSearchStream.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useSearchStream } from '@/hooks/useSearchStream';

function makeStream(lines: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    async start(controller) {
      for (const l of lines) controller.enqueue(encoder.encode(l + '\n'));
      controller.close();
    },
  });
}

describe('useSearchStream', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('parses tool_call_start, source, text, done', async () => {
    const stream = makeStream([
      JSON.stringify({ type: 'tool_call_start', tool: 'web_search', args: { query: 'x' } }),
      JSON.stringify({ type: 'source', url: 'https://a', title: 'A', summary: 's', query: 'x' }),
      JSON.stringify({ type: 'text', delta: 'Hello ' }),
      JSON.stringify({ type: 'text', delta: 'world.' }),
      JSON.stringify({ type: 'done', answer: 'Hello world.' }),
    ]);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      body: stream,
    }));

    const { result } = renderHook(() => useSearchStream());
    await act(async () => { await result.current.send(1, 'hello'); });

    await waitFor(() => expect(result.current.status).toBe('done'));
    expect(result.current.answer).toBe('Hello world.');
    expect(result.current.sources.map(s => s.url)).toEqual(['https://a']);
  });

  it('captures errors', async () => {
    const stream = makeStream([
      JSON.stringify({ type: 'error', message: 'boom' }),
    ]);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, body: stream }));
    const { result } = renderHook(() => useSearchStream());
    await act(async () => { await result.current.send(1, 'x'); });
    await waitFor(() => expect(result.current.status).toBe('error'));
    expect(result.current.error).toBe('boom');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd web-next && npm run test -- useSearchStream && cd ..
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the hook**

Create `web-next/hooks/useSearchStream.ts`:

```typescript
'use client';
import { useCallback, useRef, useState } from 'react';
import type { MemoryHit, SearchEvent, Source } from '@/lib/search';

export type StreamStatus = 'idle' | 'streaming' | 'done' | 'error';

export type StreamState = {
  status: StreamStatus;
  activeTool: { tool: string; args: Record<string, unknown> } | null;
  sources: Source[];
  memoryHits: MemoryHit[];
  answer: string;
  error: string | null;
};

const initial: StreamState = {
  status: 'idle',
  activeTool: null,
  sources: [],
  memoryHits: [],
  answer: '',
  error: null,
};

export function useSearchStream() {
  const [state, setState] = useState<StreamState>(initial);
  const abortRef = useRef<AbortController | null>(null);

  const reset = useCallback(() => setState(initial), []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  const send = useCallback(async (sessionId: number, content: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setState({ ...initial, status: 'streaming' });

    try {
      const res = await fetch(`/api/search/sessions/${sessionId}/messages`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        const detail = await res.text().catch(() => '');
        setState((s) => ({ ...s, status: 'error', error: detail || res.statusText }));
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;
          try {
            const ev = JSON.parse(trimmed) as SearchEvent;
            setState((s) => apply(s, ev));
          } catch {
            // ignore malformed line
          }
        }
      }
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : 'Stream failed';
      if (controller.signal.aborted) return;
      setState((s) => ({ ...s, status: 'error', error: message }));
    }
  }, []);

  return { ...state, send, stop, reset };
}

function apply(s: StreamState, ev: SearchEvent): StreamState {
  switch (ev.type) {
    case 'tool_call_start':
      return { ...s, activeTool: { tool: ev.tool, args: ev.args } };
    case 'source':
      return { ...s, sources: [...s.sources, { url: ev.url, title: ev.title, summary: ev.summary, query: ev.query }] };
    case 'memory_hit':
      return { ...s, memoryHits: [...s.memoryHits, { entity: ev.entity, facts: ev.facts }] };
    case 'text':
      return { ...s, answer: s.answer + ev.delta };
    case 'done':
      return { ...s, status: 'done', activeTool: null };
    case 'error':
      return { ...s, status: 'error', error: ev.message, activeTool: null };
    default:
      return s;
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd web-next && npm run test -- useSearchStream && cd ..
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add web-next/hooks/useSearchStream.ts web-next/__tests__/useSearchStream.test.ts
git commit -m "feat(web-next): add useSearchStream hook"
```

---

## Task 14: Implement source list, memory hits, and message bubble components

**Files:**
- Create: `web-next/components/search/source-list.tsx`
- Create: `web-next/components/search/memory-hits.tsx`
- Create: `web-next/components/search/message-bubble.tsx`

- [ ] **Step 1: `source-list.tsx`**

Create `web-next/components/search/source-list.tsx`:

```tsx
'use client';
import { useState } from 'react';
import type { Source } from '@/lib/search';

export function SourceList({ sources }: { sources: Source[] }) {
  const [open, setOpen] = useState(true);
  if (sources.length === 0) return null;
  return (
    <div className="mt-3 rounded border bg-card">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium hover:bg-secondary"
      >
        <span>Sources ({sources.length})</span>
        <span className="text-muted-foreground">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <ul className="divide-y">
          {sources.map((s, i) => (
            <li key={`${s.url}-${i}`} className="px-3 py-2 text-sm">
              <div className="flex items-baseline justify-between gap-2">
                <a
                  href={s.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-medium text-primary hover:underline"
                >
                  [{i + 1}] {s.title}
                </a>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {hostname(s.url)}
                </span>
              </div>
              {s.summary && (
                <p className="mt-1 line-clamp-2 text-muted-foreground">{s.summary}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function hostname(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, ''); }
  catch { return url; }
}
```

- [ ] **Step 2: `memory-hits.tsx`**

Create `web-next/components/search/memory-hits.tsx`:

```tsx
'use client';
import { useState } from 'react';
import type { MemoryHit } from '@/lib/search';

export function MemoryHits({ hits }: { hits: MemoryHit[] }) {
  const [open, setOpen] = useState(true);
  if (hits.length === 0) return null;
  return (
    <div className="mt-3 rounded border bg-card">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium hover:bg-secondary"
      >
        <span>From team memory ({hits.length})</span>
        <span className="text-muted-foreground">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <ul className="divide-y">
          {hits.map((h, i) => (
            <li key={`${h.entity.id}-${i}`} className="px-3 py-2 text-sm">
              <div className="font-medium">
                <span className="text-xs uppercase text-muted-foreground mr-2">
                  {h.entity.type}
                </span>
                {h.entity.name}
              </div>
              <ul className="ml-2 mt-1 list-disc pl-4 text-muted-foreground">
                {h.facts.map((f) => (
                  <li key={f.id}>{f.content}</li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 3: `message-bubble.tsx`**

Create `web-next/components/search/message-bubble.tsx`:

```tsx
'use client';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { MemoryHit, SearchMessage, Source } from '@/lib/search';
import { SourceList } from './source-list';
import { MemoryHits } from './memory-hits';

function extractSources(events: SearchMessage['tool_events']): Source[] {
  return events.filter((e): e is Source & { type: 'source' } => e.type === 'source')
    .map(({ url, title, summary, query }) => ({ url, title, summary, query }));
}
function extractHits(events: SearchMessage['tool_events']): MemoryHit[] {
  return events.filter((e): e is MemoryHit & { type: 'memory_hit' } => e.type === 'memory_hit')
    .map(({ entity, facts }) => ({ entity, facts }));
}

export function MessageBubble({ message }: { message: SearchMessage }) {
  if (message.role === 'user') {
    return (
      <div className="ml-auto max-w-[80%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">
        {message.content}
      </div>
    );
  }
  const sources = extractSources(message.tool_events);
  const hits = extractHits(message.tool_events);
  return (
    <div className="max-w-[90%]">
      <div className="prose prose-sm max-w-none rounded-lg bg-card px-3 py-2">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
      </div>
      <MemoryHits hits={hits} />
      <SourceList sources={sources} />
    </div>
  );
}
```

- [ ] **Step 4: Typecheck**

```bash
cd web-next && npm run typecheck && cd ..
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/search/
git commit -m "feat(web-next): add source list, memory hits, message bubble"
```

---

## Task 15: Implement composer, chat pane, session list, and page

**Files:**
- Create: `web-next/components/search/composer.tsx`
- Create: `web-next/components/search/chat-pane.tsx`
- Create: `web-next/components/search/session-list.tsx`
- Create: `web-next/app/(app)/search/page.tsx`

- [ ] **Step 1: `composer.tsx`**

Create `web-next/components/search/composer.tsx`:

```tsx
'use client';
import { useState, KeyboardEvent } from 'react';
import { Button } from '@/components/ui/button';

export function Composer({
  disabled,
  onSubmit,
  onStop,
  streaming,
}: {
  disabled?: boolean;
  onSubmit: (content: string) => void;
  onStop?: () => void;
  streaming?: boolean;
}) {
  const [text, setText] = useState('');

  const submit = () => {
    const t = text.trim();
    if (!t || disabled) return;
    onSubmit(t);
    setText('');
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="flex items-end gap-2 border-t p-3">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        rows={2}
        placeholder="Ask anything…"
        className="flex-1 resize-none rounded border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
        disabled={disabled}
      />
      {streaming && onStop ? (
        <Button variant="secondary" onClick={onStop}>Stop</Button>
      ) : (
        <Button onClick={submit} disabled={disabled || !text.trim()}>Send</Button>
      )}
    </div>
  );
}
```

- [ ] **Step 2: `chat-pane.tsx`**

Create `web-next/components/search/chat-pane.tsx`:

```tsx
'use client';
import { useEffect, useRef, useState } from 'react';
import { getSession, type SearchMessage } from '@/lib/search';
import { useSearchStream } from '@/hooks/useSearchStream';
import { MessageBubble } from './message-bubble';
import { Composer } from './composer';
import { SourceList } from './source-list';
import { MemoryHits } from './memory-hits';

export function ChatPane({
  sessionId,
  onTitleChange,
}: {
  sessionId: number;
  onTitleChange?: (title: string) => void;
}) {
  const [messages, setMessages] = useState<SearchMessage[]>([]);
  const stream = useSearchStream();
  const scrollRef = useRef<HTMLDivElement>(null);

  // Load history when session changes
  useEffect(() => {
    let cancelled = false;
    stream.reset();
    getSession(sessionId).then((s) => {
      if (cancelled) return;
      setMessages(s.messages);
      onTitleChange?.(s.title);
    }).catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Refresh after a turn finishes
  useEffect(() => {
    if (stream.status !== 'done') return;
    getSession(sessionId).then((s) => {
      setMessages(s.messages);
      onTitleChange?.(s.title);
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream.status]);

  // Auto-scroll on any change
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, stream.answer, stream.sources.length, stream.memoryHits.length]);

  const send = async (content: string) => {
    // Optimistically render the user message
    setMessages((m) => [
      ...m,
      {
        id: -Date.now(),
        role: 'user',
        content,
        tool_events: [],
        truncated: false,
        created_at: new Date().toISOString(),
      },
    ]);
    await stream.send(sessionId, content);
  };

  const streaming = stream.status === 'streaming';

  return (
    <div className="flex h-full flex-col">
      <div ref={scrollRef} className="flex-1 space-y-4 overflow-auto p-4">
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}

        {streaming && (
          <div className="max-w-[90%]">
            {stream.activeTool && (
              <div className="mb-2 text-xs text-muted-foreground">
                {labelForTool(stream.activeTool.tool, stream.activeTool.args)}
              </div>
            )}
            <MemoryHits hits={stream.memoryHits} />
            <SourceList sources={stream.sources} />
            {stream.answer && (
              <div className="prose prose-sm mt-3 max-w-none rounded-lg bg-card px-3 py-2 whitespace-pre-wrap">
                {stream.answer}
              </div>
            )}
            {!stream.answer && !stream.activeTool && (
              <div className="text-sm text-muted-foreground">Thinking…</div>
            )}
          </div>
        )}

        {stream.status === 'error' && (
          <div className="rounded border border-destructive bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {stream.error}
          </div>
        )}
      </div>
      <Composer
        onSubmit={send}
        onStop={stream.stop}
        streaming={streaming}
        disabled={streaming}
      />
    </div>
  );
}

function labelForTool(tool: string, args: Record<string, unknown>): string {
  switch (tool) {
    case 'web_search': return `Searching: ${args.query ?? ''}`;
    case 'fetch_url': return `Reading ${args.url ?? ''}`;
    case 'recall_memory': return `Recalling team memory: ${args.query ?? ''}`;
    case 'remember_memory': return `Saving to team memory…`;
    default: return `Running ${tool}…`;
  }
}
```

- [ ] **Step 3: `session-list.tsx`**

Create `web-next/components/search/session-list.tsx`:

```tsx
'use client';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { SearchSession } from '@/lib/search';
import { deleteSession } from '@/lib/search';

export function SessionList({
  sessions,
  activeId,
  onSelect,
  onNew,
  onDeleted,
}: {
  sessions: SearchSession[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
  onDeleted: (id: number) => void;
}) {
  return (
    <aside className="flex h-full w-64 flex-col border-r bg-card">
      <div className="p-2">
        <Button className="w-full" onClick={onNew}>+ New search</Button>
      </div>
      <ul className="flex-1 overflow-auto">
        {sessions.map((s) => (
          <li
            key={s.id}
            className={cn(
              'group flex items-center justify-between gap-2 px-3 py-2 text-sm hover:bg-secondary cursor-pointer',
              activeId === s.id && 'bg-secondary',
            )}
            onClick={() => onSelect(s.id)}
          >
            <span className="truncate">{s.title}</span>
            <button
              type="button"
              className="opacity-0 group-hover:opacity-100 text-xs text-muted-foreground hover:text-destructive"
              onClick={async (e) => {
                e.stopPropagation();
                await deleteSession(s.id).catch(() => {});
                onDeleted(s.id);
              }}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}
```

- [ ] **Step 4: `page.tsx`**

Create `web-next/app/(app)/search/page.tsx`:

```tsx
'use client';
import { useEffect, useState } from 'react';
import { ChatPane } from '@/components/search/chat-pane';
import { SessionList } from '@/components/search/session-list';
import {
  createSession,
  listSessions,
  type SearchSession,
} from '@/lib/search';

export default function SearchPage() {
  const [sessions, setSessions] = useState<SearchSession[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);

  const refresh = async () => {
    const ss = await listSessions().catch(() => []);
    setSessions(ss);
    if (ss.length && activeId == null) setActiveId(ss[0].id);
  };

  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const onNew = async () => {
    const s = await createSession();
    setSessions((cur) => [s, ...cur]);
    setActiveId(s.id);
  };

  const onDeleted = (id: number) => {
    setSessions((cur) => cur.filter((s) => s.id !== id));
    if (activeId === id) setActiveId(null);
  };

  const onTitleChange = (title: string) => {
    setSessions((cur) => cur.map((s) => (s.id === activeId ? { ...s, title } : s)));
  };

  return (
    <div className="flex h-full">
      <SessionList
        sessions={sessions}
        activeId={activeId}
        onSelect={setActiveId}
        onNew={onNew}
        onDeleted={onDeleted}
      />
      <section className="flex-1 overflow-hidden">
        {activeId == null ? (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-muted-foreground">
              Click <strong>+ New search</strong> to start.
            </p>
          </div>
        ) : (
          <ChatPane key={activeId} sessionId={activeId} onTitleChange={onTitleChange} />
        )}
      </section>
    </div>
  );
}
```

- [ ] **Step 5: Typecheck**

```bash
cd web-next && npm run typecheck && cd ..
```

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add web-next/components/search/composer.tsx web-next/components/search/chat-pane.tsx \
        web-next/components/search/session-list.tsx web-next/app/\(app\)/search/page.tsx
git commit -m "feat(web-next): add Search page, chat pane, session list, composer"
```

---

## Task 16: End-to-end smoke and verification

**Files:** none

- [ ] **Step 1: Run the full backend suite + lint**

```bash
.venv/bin/python3 -m pytest tests/ -q
ruff check .
```

Expected: all green.

- [ ] **Step 2: Run the frontend tests + build**

```bash
cd web-next && npm run test && npm run typecheck && npm run build && cd ..
```

Expected: all green; `next build` succeeds.

- [ ] **Step 3: Manual smoke**

```bash
# Set BRAVE_API_KEY in .env first
docker compose up -d --build
docker compose exec auto-agent alembic upgrade head
```

In a browser at http://localhost:3000:
1. Sign in.
2. Click **Search** in the sidebar.
3. Click **+ New search**.
4. Ask: "What's the latest stable Python release?"
5. Verify: a `Searching: …` indicator appears, sources populate above the answer (no thumbnails/favicons), the answer streams in below, the source list expands/collapses.
6. Ask a follow-up that references the prior turn ("when was that released?") — verify multi-turn context.
7. Ask: "Remember that I prefer concise answers." — verify the agent calls `remember_memory` and the activity strip says "Saving to team memory…".
8. Reload the page — verify the session shows in the left rail with its auto-generated title and the message history is intact.
9. Delete the session — verify it disappears.

- [ ] **Step 4: Final commit (if any straggler fixes were needed)**

```bash
git status
# If clean, skip the commit
```

---

## Self-review notes

- **Spec coverage:** Each spec section maps to a task — data model (1), config (2), `web_search` (4), `fetch_url` (5), `recall_memory` (6), `remember_memory` (7), `search_loop` (8), title (9), endpoints (10), sidebar (11), REST helpers (12), hook (13), components (14, 15), smoke (16). Pre-turn recall is wired in Task 8. The `tool_events` JSON column captures sources + memory_hits for replay (Task 1, used in Task 14's `MessageBubble`).
- **Placeholder scan:** No TBDs, all code shown. `react-markdown` is added explicitly in Task 11.
- **Type consistency:** `Source`, `MemoryHit`, `SearchEvent`, `SearchMessage` defined in `lib/search.ts` (Task 12) and consumed identically in the hook (Task 13) and components (Task 14, 15). Backend `tool_events` is a list of dicts that round-trips to the frontend `SearchEvent[]` shape via the same field names (`type`, `url`, `title`, etc.).
- **Known compromise:** Task 8 monkey-patches each tool's `execute` to inject `event_sink` into the `ToolContext` because `agent/loop.py` constructs `ToolContext` internally. A cleaner alternative would be to add an `event_sink` parameter to `AgentLoop.__init__`, but that touches the existing loop and risks regression for the Tasks/Freeform flows. Task 3 already extends `ToolContext`; the patch in Task 8 just plumbs the sink through. If reviewers prefer, follow-up cleanup is to thread the sink through `AgentLoop` directly.
