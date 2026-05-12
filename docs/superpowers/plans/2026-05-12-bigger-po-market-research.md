# Bigger PO + Market Research — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Product Owner cron produce evidence-backed, market-aware suggestions instead of button-sized polish. Implements sub-project A from `docs/superpowers/specs/2026-05-12-bigger-po-market-research-design.md`.

**Architecture:** A new `agent/market_researcher.py` runs inline before the existing PO analyzer when its brief is stale. The researcher writes a versioned `MarketBrief` row using web tools (Brave Search + URL fetch). The PO then runs with the brief rendered into its prompt and a post-parse filter that drops any non-`bug` suggestion without cited evidence.

**Tech Stack:** Python 3.12 async + SQLAlchemy + Alembic + structlog + httpx (existing), Brave Search API (existing `agent/tools/web_search.py`), html2text+BeautifulSoup (existing `agent/tools/fetch_url.py`), Next.js App Router + TanStack Query + Tailwind + shadcn/ui (web-next).

---

## File map

**New files:**
- `agent/market_researcher.py` — researcher loop, mirrors `agent/po_analyzer.py` shape, with `run_market_research` helper.
- `migrations/versions/030_market_research.py` — Alembic migration for `market_briefs` table + `freeform_configs` and `suggestions` additions.
- `tests/test_market_brief_freshness.py` — pure logic test of `_brief_is_fresh`.
- `tests/test_market_researcher.py` — researcher in isolation, stubbed LLM + web tools.
- `tests/test_po_with_market_research.py` — chain integration test.
- `tests/test_po_drops_ungrounded_suggestions.py` — regression test (the load-bearing one).
- `web-next/components/market-brief/market-brief-modal.tsx` — modal showing brief contents.
- `web-next/lib/market-brief.ts` — TanStack Query hook + fetcher for the new endpoint.

**Modified files:**
- `shared/models.py` — new `MarketBrief` model, columns on `Suggestion` + `FreeformConfig`.
- `shared/events.py` — `POEventType` gets 3 market-research entries + 3 factory functions.
- `agent/lifecycle/_orchestrator_api.py` — register new event types in the broadcast routing.
- `agent/context/workspace_state.py` — track `url_fetches` so `raw_sources` can be derived deterministically.
- `agent/tools/__init__.py` — `create_default_registry` gains a `with_web: bool = False` flag.
- `agent/prompts.py` — new `MARKET_RESEARCH_PROMPT` + `build_market_research_prompt`; modified `PO_ANALYSIS_PROMPT` + `build_po_analysis_prompt` (brief required).
- `agent/po_analyzer.py` — `_check_and_analyze` orchestrates chain; `handle_po_analysis` takes required `brief` + applies post-parse filter; `_brief_is_fresh` helper.
- `orchestrator/router.py` — `GET /api/repos/{repo_id}/market-brief/latest` endpoint.
- `web-next/app/(app)/suggestions/page.tsx` — render "Backed by" footer when `evidence_urls` non-empty; header link opens the brief modal.

---

## Task 1: Extend `WorkspaceState` to track URL fetches

**Files:**
- Modify: `agent/context/workspace_state.py`
- Test: `tests/test_workspace_state.py` (existing — extend, or create if missing)

**Why this task:** The spec says `raw_sources` is collected from `WorkspaceState`, not from the agent's own JSON output (to avoid the "agent drops the URL list under context pressure" failure mode). `WorkspaceState` currently tracks only file reads/writes and bash commands. We need a small extension.

- [ ] **Step 1: Check if `tests/test_workspace_state.py` exists; if not, create with this header**

```bash
ls tests/test_workspace_state.py 2>/dev/null || echo "MISSING"
```

If missing, create with:
```python
"""Tests for agent/context/workspace_state.py."""
from agent.context.workspace_state import WorkspaceState
```

- [ ] **Step 2: Write the failing test for URL fetch tracking**

Add to `tests/test_workspace_state.py`:
```python
def test_fetch_url_call_recorded():
    state = WorkspaceState()
    state.process_tool_call(
        "fetch_url", {"url": "https://example.com/features"}
    )
    assert len(state.url_fetches) == 1
    assert state.url_fetches[0]["url"] == "https://example.com/features"
    assert state.url_fetches[0]["turn"] == 0


def test_web_search_call_does_not_record_url_fetch():
    state = WorkspaceState()
    state.process_tool_call("web_search", {"query": "ai dev tools"})
    assert state.url_fetches == []
```

- [ ] **Step 3: Run test to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_workspace_state.py::test_fetch_url_call_recorded -v
```

Expected: FAIL — `AttributeError: 'WorkspaceState' object has no attribute 'url_fetches'`.

- [ ] **Step 4: Implement the change in `agent/context/workspace_state.py`**

In the `WorkspaceState` dataclass, add the field next to `bash_commands`:
```python
    url_fetches: list[dict[str, Any]] = field(default_factory=list)
```

Add to `process_tool_call`, after the `bash` branch and before `return warning`:
```python
        elif tool_name == "fetch_url":
            url = arguments.get("url", "")
            if url:
                self.url_fetches.append(
                    {"url": url, "turn": self.current_turn}
                )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_workspace_state.py -v
```

Expected: both new tests PASS, all pre-existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/context/workspace_state.py tests/test_workspace_state.py
git commit -m "feat(workspace_state): track fetch_url tool calls for source provenance"
```

---

## Task 2: Add `with_web` flag to the default tool registry

**Files:**
- Modify: `agent/tools/__init__.py`
- Test: `tests/test_default_registry.py` (existing — extend, or create if missing)

**Why this task:** `WebSearchTool` and `FetchUrlTool` exist but are not registered by `create_default_registry`. The researcher needs them. Adding an explicit `with_web: bool = False` keyword keeps the default behavior unchanged for all existing callers.

- [ ] **Step 1: Check if `tests/test_default_registry.py` exists**

```bash
ls tests/test_default_registry.py 2>/dev/null || echo "MISSING"
```

If missing, create with:
```python
"""Tests for agent/tools/__init__.py."""
from agent.tools import create_default_registry
```

- [ ] **Step 2: Write failing tests**

Add to `tests/test_default_registry.py`:
```python
def test_default_registry_excludes_web_tools_by_default():
    reg = create_default_registry(readonly=True)
    names = {t.name for t in reg.all()}
    assert "web_search" not in names
    assert "fetch_url" not in names


def test_default_registry_includes_web_tools_when_requested():
    reg = create_default_registry(readonly=True, with_web=True)
    names = {t.name for t in reg.all()}
    assert "web_search" in names
    assert "fetch_url" in names
```

(If `ToolRegistry` has no `all()` method, look in `agent/tools/base.py` for the equivalent — likely `registry.tools.values()` or `iter(registry)`. Adjust the assertion accordingly.)

- [ ] **Step 3: Run tests to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_default_registry.py -v
```

Expected: `test_default_registry_includes_web_tools_when_requested` FAILS — `with_web` is not a recognized kwarg.

- [ ] **Step 4: Modify `agent/tools/__init__.py`**

Add the imports near the existing tool imports:
```python
from agent.tools.fetch_url import FetchUrlTool
from agent.tools.web_search import WebSearchTool
```

Change the function signature and body:
```python
def create_default_registry(
    readonly: bool = False, with_web: bool = False
) -> ToolRegistry:
    """Create a registry with all standard coding tools.

    Args:
        readonly: If True, exclude tools that modify files (planning mode).
        with_web: If True, include web_search + fetch_url (researcher mode).
    """
    registry = ToolRegistry()

    registry.register(FileReadTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(GitTool())
    registry.register(SkillTool())
    registry.register(RecallMemoryTool())

    if with_web:
        registry.register(WebSearchTool())
        registry.register(FetchUrlTool())

    if not readonly:
        registry.register(FileWriteTool())
        registry.register(FileEditTool())
        registry.register(BashTool())
        registry.register(TestRunnerTool())
        registry.register(SubagentTool())
        registry.register(RememberMemoryTool())

    return registry
```

- [ ] **Step 5: Pass `with_web` through `create_agent`**

In `agent/lifecycle/factory.py`, find `def create_agent(...)` and add `with_web: bool = False` to its parameter list (alongside `readonly`). Find the call site `tools = create_default_registry(readonly=readonly)` and change to `tools = create_default_registry(readonly=readonly, with_web=with_web)`.

- [ ] **Step 6: Run tests + the broader test suite to verify**

```bash
.venv/bin/python3 -m pytest tests/test_default_registry.py -v
.venv/bin/python3 -m pytest tests/ -q
```

Expected: new tests PASS, pre-existing tests still PASS.

- [ ] **Step 7: Commit**

```bash
git add agent/tools/__init__.py agent/lifecycle/factory.py tests/test_default_registry.py
git commit -m "feat(tools): add with_web flag to default registry for researcher agent"
```

---

## Task 3: Alembic migration for the schema additions

**Files:**
- Create: `migrations/versions/030_market_research.py`

**Why this task:** Schema needs to land before the ORM model changes so the test DB can be brought up to head. All columns nullable or defaulted — no backfill needed.

- [ ] **Step 1: Create the migration file**

```python
"""Market research — MarketBrief table + Suggestion.evidence_urls + FreeformConfig age.

Adds the schema for sub-project A of the PO/freeform overhaul. See
docs/superpowers/specs/2026-05-12-bigger-po-market-research-design.md.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_briefs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "repo_id",
            sa.Integer,
            sa.ForeignKey("repos.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("product_category", sa.Text, nullable=True),
        sa.Column(
            "competitors",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "findings",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "modality_gaps",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "strategic_themes",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "raw_sources",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "partial", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("agent_turns", sa.Integer, nullable=False, server_default="0"),
    )

    op.add_column(
        "freeform_configs",
        sa.Column(
            "last_market_research_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "freeform_configs",
        sa.Column(
            "market_brief_max_age_days",
            sa.Integer,
            nullable=False,
            server_default="7",
        ),
    )

    op.add_column(
        "suggestions",
        sa.Column(
            "evidence_urls",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "suggestions",
        sa.Column(
            "brief_id",
            sa.Integer,
            sa.ForeignKey("market_briefs.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("suggestions", "brief_id")
    op.drop_column("suggestions", "evidence_urls")
    op.drop_column("freeform_configs", "market_brief_max_age_days")
    op.drop_column("freeform_configs", "last_market_research_at")
    op.drop_table("market_briefs")
```

- [ ] **Step 2: Verify the migration applies cleanly**

```bash
docker compose up -d
docker compose exec auto-agent alembic upgrade head
```

Expected: `030_market_research` runs without error. If you don't have docker running, alembic can also be run locally if the DB env vars are set: `.venv/bin/alembic upgrade head`.

- [ ] **Step 3: Verify downgrade also works**

```bash
docker compose exec auto-agent alembic downgrade -1
docker compose exec auto-agent alembic upgrade head
```

Expected: downgrade succeeds, upgrade re-applies cleanly.

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/030_market_research.py
git commit -m "feat(db): migration 030 — market_briefs table + Suggestion.evidence_urls"
```

---

## Task 4: ORM models — `MarketBrief` + additions to `Suggestion` / `FreeformConfig`

**Files:**
- Modify: `shared/models.py`
- Test: `tests/test_models_market_brief.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_models_market_brief.py`:
```python
"""Smoke test the new MarketBrief ORM model + extended Suggestion/FreeformConfig."""

import pytest
from sqlalchemy import select

from shared.database import async_session
from shared.models import (
    FreeformConfig,
    MarketBrief,
    Repo,
    Suggestion,
    SuggestionStatus,
)


@pytest.mark.asyncio
async def test_market_brief_round_trips_through_db(test_org_id, test_repo):
    async with async_session() as session:
        brief = MarketBrief(
            repo_id=test_repo.id,
            organization_id=test_org_id,
            product_category="AI dev tools",
            competitors=[{"name": "X", "url": "https://x.example", "why_relevant": "y"}],
            findings=[{"theme": "agents", "observation": "lots", "sources": ["https://x.example"]}],
            modality_gaps=[],
            strategic_themes=[],
            summary="short",
            raw_sources=[{"url": "https://x.example", "title": "X", "fetched_at": "2026-05-12"}],
            partial=False,
            agent_turns=12,
        )
        session.add(brief)
        await session.commit()

        loaded = (
            await session.execute(select(MarketBrief).where(MarketBrief.id == brief.id))
        ).scalar_one()
        assert loaded.product_category == "AI dev tools"
        assert loaded.competitors[0]["name"] == "X"
        assert loaded.findings[0]["sources"] == ["https://x.example"]
        assert loaded.partial is False
        assert loaded.agent_turns == 12


@pytest.mark.asyncio
async def test_suggestion_carries_brief_link_and_evidence(test_org_id, test_repo):
    async with async_session() as session:
        brief = MarketBrief(
            repo_id=test_repo.id, organization_id=test_org_id,
        )
        session.add(brief)
        await session.flush()
        s = Suggestion(
            repo_id=test_repo.id,
            organization_id=test_org_id,
            title="Add voice input",
            description="...",
            rationale="...",
            category="feature",
            priority=2,
            status=SuggestionStatus.PENDING,
            evidence_urls=[
                {"url": "https://x.example", "title": "X", "excerpt": "supports voice"}
            ],
            brief_id=brief.id,
        )
        session.add(s)
        await session.commit()

        loaded = (
            await session.execute(select(Suggestion).where(Suggestion.id == s.id))
        ).scalar_one()
        assert loaded.brief_id == brief.id
        assert loaded.evidence_urls[0]["url"] == "https://x.example"


@pytest.mark.asyncio
async def test_freeform_config_has_market_brief_age_default(test_org_id, test_repo):
    async with async_session() as session:
        cfg = FreeformConfig(repo_id=test_repo.id, organization_id=test_org_id)
        session.add(cfg)
        await session.commit()

        loaded = (
            await session.execute(
                select(FreeformConfig).where(FreeformConfig.id == cfg.id)
            )
        ).scalar_one()
        assert loaded.market_brief_max_age_days == 7
        assert loaded.last_market_research_at is None
```

(If the `test_org_id` / `test_repo` fixtures don't exist in your `tests/conftest.py`, check what fixtures the existing `tests/test_architecture_mode.py` or `tests/test_org_scoping_coverage.py` use and reuse those — do not invent new ones.)

- [ ] **Step 2: Run tests to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_models_market_brief.py -v
```

Expected: FAIL — `MarketBrief` doesn't exist; `Suggestion.evidence_urls` / `Suggestion.brief_id` / `FreeformConfig.market_brief_max_age_days` missing.

- [ ] **Step 3: Add `MarketBrief` to `shared/models.py`**

Insert this class above `class Suggestion(Base)`:
```python
class MarketBrief(Base):
    """Versioned market-research brief produced by the market_researcher agent.

    Consumed by the PO analyzer to ground its suggestions in cited evidence.
    """
    __tablename__ = "market_briefs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repos.id"), nullable=False, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), nullable=False, index=True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    product_category = Column(Text, nullable=True)
    competitors = Column(JSONB, default=list, nullable=False)
    findings = Column(JSONB, default=list, nullable=False)
    modality_gaps = Column(JSONB, default=list, nullable=False)
    strategic_themes = Column(JSONB, default=list, nullable=False)
    summary = Column(Text, default="", nullable=False)
    raw_sources = Column(JSONB, default=list, nullable=False)
    partial = Column(Boolean, default=False, nullable=False)
    agent_turns = Column(Integer, default=0, nullable=False)

    repo = relationship("Repo")
```

If `JSONB` is not yet imported in `shared/models.py`, add to the top:
```python
from sqlalchemy.dialects.postgresql import JSONB
```

- [ ] **Step 4: Extend `Suggestion`**

In `class Suggestion(Base)`, after `created_at = ...` add:
```python
    evidence_urls = Column(JSONB, default=list, nullable=False)
    brief_id = Column(Integer, ForeignKey("market_briefs.id"), nullable=True)
    brief = relationship("MarketBrief")
```

- [ ] **Step 5: Extend `FreeformConfig`**

In `class FreeformConfig(Base)`, alongside the other freeform-config columns, add:
```python
    last_market_research_at = Column(DateTime(timezone=True), nullable=True)
    market_brief_max_age_days = Column(Integer, default=7, nullable=False)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_models_market_brief.py -v
.venv/bin/python3 -m pytest tests/ -q
```

Expected: new tests PASS; all pre-existing tests still PASS.

- [ ] **Step 7: Commit**

```bash
git add shared/models.py tests/test_models_market_brief.py
git commit -m "feat(models): MarketBrief + Suggestion.evidence_urls + FreeformConfig.market_brief_max_age_days"
```

---

## Task 5: Event taxonomy + factories for market research

**Files:**
- Modify: `shared/events.py`
- Modify: `agent/lifecycle/_orchestrator_api.py`
- Test: `tests/test_events_market_research.py` (new) — or extend `tests/test_events_taxonomy.py` if structurally simpler.

- [ ] **Step 1: Write the failing test**

Create `tests/test_events_market_research.py`:
```python
"""Tests for market-research event factories + taxonomy."""

from shared.events import (
    POEventType,
    market_research_completed,
    market_research_failed,
    market_research_started,
)


def test_market_research_started_event_shape():
    e = market_research_started(repo_name="foo")
    assert str(e.type) == "po.market_research_started"
    assert e.payload == {"repo_name": "foo"}


def test_market_research_completed_event_shape():
    e = market_research_completed(
        repo_name="foo", brief_id=42, n_competitors=4, n_findings=7, partial=False,
    )
    assert str(e.type) == "po.market_research_completed"
    assert e.payload == {
        "repo_name": "foo",
        "brief_id": 42,
        "n_competitors": 4,
        "n_findings": 7,
        "partial": False,
    }


def test_market_research_failed_includes_reason():
    e = market_research_failed(repo_name="foo", reason="brave key missing")
    assert str(e.type) == "po.market_research_failed"
    assert e.payload == {"repo_name": "foo", "reason": "brave key missing"}


def test_market_research_failed_omits_blank_reason():
    e = market_research_failed(repo_name="foo")
    assert e.payload == {"repo_name": "foo"}


def test_market_research_types_registered_in_po_enum():
    assert POEventType.MARKET_RESEARCH_STARTED == "po.market_research_started"
    assert POEventType.MARKET_RESEARCH_COMPLETED == "po.market_research_completed"
    assert POEventType.MARKET_RESEARCH_FAILED == "po.market_research_failed"
```

- [ ] **Step 2: Run test to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_events_market_research.py -v
```

Expected: FAIL — names not defined.

- [ ] **Step 3: Extend `POEventType` and add factories**

In `shared/events.py`, add three members to `POEventType`:
```python
class POEventType(StrEnum):
    ANALYZE = "po.analyze"
    ANALYSIS_QUEUED = "po.analysis_queued"
    ANALYSIS_STARTED = "po.analysis_started"
    ANALYSIS_FAILED = "po.analysis_failed"
    SUGGESTIONS_READY = "po.suggestions_ready"
    MARKET_RESEARCH_STARTED = "po.market_research_started"
    MARKET_RESEARCH_COMPLETED = "po.market_research_completed"
    MARKET_RESEARCH_FAILED = "po.market_research_failed"
```

Add the factory functions next to `po_suggestions_ready`:
```python
def market_research_started(repo_name: str) -> Event:
    return Event(
        type=POEventType.MARKET_RESEARCH_STARTED,
        task_id=0,
        payload={"repo_name": repo_name},
    )


def market_research_completed(
    repo_name: str,
    brief_id: int,
    n_competitors: int,
    n_findings: int,
    partial: bool,
) -> Event:
    return Event(
        type=POEventType.MARKET_RESEARCH_COMPLETED,
        task_id=0,
        payload={
            "repo_name": repo_name,
            "brief_id": brief_id,
            "n_competitors": n_competitors,
            "n_findings": n_findings,
            "partial": partial,
        },
    )


def market_research_failed(repo_name: str, reason: str = "") -> Event:
    payload: dict[str, Any] = {"repo_name": repo_name}
    if reason:
        payload["reason"] = reason
    return Event(
        type=POEventType.MARKET_RESEARCH_FAILED, task_id=0, payload=payload,
    )
```

- [ ] **Step 4: Register the new event types in the broadcast routing**

Open `agent/lifecycle/_orchestrator_api.py` and find where the existing `POEventType.ANALYSIS_STARTED` (or `"po.analysis_started"`) is matched/routed for the websocket broadcast. Add the three new event types alongside them. (The exact addition is a one-line tuple/list entry — search for `po.analysis_started` and add the three new wire strings in the same place.)

If `_orchestrator_api.py` uses a glob pattern like `"po.*"` for the WS subscription, no change is needed — the new events fall under that pattern automatically. In that case skip this step but verify with:
```bash
grep -n "po\." agent/lifecycle/_orchestrator_api.py
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_events_market_research.py -v
.venv/bin/python3 -m pytest tests/test_events_taxonomy.py -v
```

Expected: new tests PASS; existing event-taxonomy tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add shared/events.py agent/lifecycle/_orchestrator_api.py tests/test_events_market_research.py
git commit -m "feat(events): market_research_{started,completed,failed} factories"
```

---

## Task 6: `_brief_is_fresh` pure helper

**Files:**
- Modify: `agent/po_analyzer.py` (add helper)
- Create: `tests/test_market_brief_freshness.py`

**Why this task:** The freshness check is pure logic. Extracted as a free function for trivial testability with no DB / time mocking.

- [ ] **Step 1: Write the failing test**

Create `tests/test_market_brief_freshness.py`:
```python
"""Tests for agent.po_analyzer._brief_is_fresh."""

from datetime import UTC, datetime, timedelta

from agent.po_analyzer import _brief_is_fresh


def test_none_brief_is_not_fresh():
    now = datetime(2026, 5, 12, tzinfo=UTC)
    assert _brief_is_fresh(None, now, max_age_days=7) is False


def test_brief_within_max_age_is_fresh():
    now = datetime(2026, 5, 12, tzinfo=UTC)
    created_at = now - timedelta(days=3)
    assert _brief_is_fresh_for_test(created_at, now, 7) is True


def test_brief_at_exact_age_is_not_fresh():
    now = datetime(2026, 5, 12, tzinfo=UTC)
    created_at = now - timedelta(days=7)
    assert _brief_is_fresh_for_test(created_at, now, 7) is False


def test_brief_older_than_max_age_is_not_fresh():
    now = datetime(2026, 5, 12, tzinfo=UTC)
    created_at = now - timedelta(days=8)
    assert _brief_is_fresh_for_test(created_at, now, 7) is False


def _brief_is_fresh_for_test(created_at, now, max_age_days):
    """Build a minimal duck-typed brief; the real function only reads .created_at."""
    class FakeBrief:
        pass
    b = FakeBrief()
    b.created_at = created_at
    return _brief_is_fresh(b, now, max_age_days)
```

- [ ] **Step 2: Run test to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_market_brief_freshness.py -v
```

Expected: FAIL — `_brief_is_fresh` not defined.

- [ ] **Step 3: Add the helper to `agent/po_analyzer.py`**

After `_is_due(...)` add:
```python
def _brief_is_fresh(
    brief, now: datetime, max_age_days: int
) -> bool:
    """True if `brief` exists and is younger than `max_age_days`.

    The duck-typed signature (any object with `.created_at`) keeps the test
    boundary clean — no DB or ORM dependency.
    """
    if brief is None:
        return False
    created = brief.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now - created) < timedelta(days=max_age_days)
```

Make sure `from datetime import UTC, datetime, timedelta` is in the imports of `agent/po_analyzer.py` (it already has `UTC, datetime`; add `timedelta`).

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_market_brief_freshness.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/po_analyzer.py tests/test_market_brief_freshness.py
git commit -m "feat(po_analyzer): _brief_is_fresh helper"
```

---

## Task 7: Researcher prompt + `build_market_research_prompt`

**Files:**
- Modify: `agent/prompts.py`
- Test: `tests/test_prompts_market_research.py` (new)

**Why this task:** The prompt is the surface where "no claim without a source" is enforced *and* where the package.json exclusion lives (regression for the explicit token-cost decision).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_prompts_market_research.py`:
```python
"""Tests for the market_research prompt builder."""

from agent.prompts import build_market_research_prompt


def test_market_research_prompt_renders():
    p = build_market_research_prompt(repo_name="acme-app")
    assert "acme-app" in p
    assert "STRICT JSON" in p  # output discipline


def test_market_research_prompt_excludes_package_json():
    """Regression: package.json is too long, we explicitly excluded it."""
    p = build_market_research_prompt(repo_name="acme-app")
    assert "package.json" not in p


def test_market_research_prompt_mentions_three_lenses():
    p = build_market_research_prompt(repo_name="acme-app")
    # The three lenses (competitive / modality / strategic) must be present
    # by name so the prompt doesn't drift into "look at competitors" only.
    lower = p.lower()
    assert "competitor" in lower
    assert "modality" in lower or "voice" in lower
    assert "strategic" in lower or "why now" in lower


def test_market_research_prompt_requires_citations():
    p = build_market_research_prompt(repo_name="acme-app")
    # Hard rule: no claim without a source URL.
    assert "cite" in p.lower() or "source" in p.lower()
```

- [ ] **Step 2: Run tests to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_prompts_market_research.py -v
```

Expected: FAIL — `build_market_research_prompt` not defined.

- [ ] **Step 3: Add the prompt constant + builder to `agent/prompts.py`**

Add the prompt template (near `PO_ANALYSIS_PROMPT`):
```python
MARKET_RESEARCH_PROMPT = """\
You are a market researcher producing a brief that a Product Owner agent
will read to ground its suggestions for the repo "{repo_name}".

## Phase 1 — Anchor on what the product is

Read `README.md` (first ~100 lines) and `CONTEXT.md` (if present). Then
`glob` the top-level routes/pages **by filename only** — do NOT read their
contents. Do NOT read `package.json` or any other manifest file (too long,
token-wasteful, low signal).

Output (mentally) a one-paragraph product description and an inferred
product category (e.g. "AI dev tools", "social music app", "internal
admin dashboard").

## Phase 2 — Discover competitors

Use `web_search` for the category and adjacent terms. Pick 3-5
representative competing/comparable products. For each, `fetch_url` the
landing or features page and extract what they actually offer.

## Phase 3 — Three lenses

For each lens, search the web and synthesize findings. Every claim must
carry the URL it came from.

- **Competitive lens** — what do competitors have that this repo doesn't?
- **Modality lens** — voice, vision, AI-native, multi-modal angles that
  competitors are exploring. Search "<category> voice", "<category> AI",
  etc.
- **Strategic / why-now lens** — recent launches, funding signals, public
  roadmaps, trend reports. What's the market doing right now that makes
  this product timely?

## Phase 4 — Synthesize the brief

Output a strict JSON object matching the schema below. **Hard rule:** no
claim, observation, or opportunity may appear without at least one source
URL in its `sources` field. If you cannot cite it, drop it.

## Output format (STRICT JSON — no markdown fences, no commentary)

{{
  "product_category": "Inferred category",
  "competitors": [
    {{"name": "Competitor name", "url": "https://...", "why_relevant": "..."}}
  ],
  "findings": [
    {{"theme": "...", "observation": "...", "sources": ["url1", "url2"]}}
  ],
  "modality_gaps": [
    {{"modality": "voice|vision|ai-native|multimodal|...",
      "opportunity": "...", "sources": ["url1"]}}
  ],
  "strategic_themes": [
    {{"theme": "...", "why_now": "...", "sources": ["url1"]}}
  ],
  "summary": "2-4 sentence prose digest the PO will read first."
}}

Output ONLY the JSON object. No other text.
"""


def build_market_research_prompt(repo_name: str) -> str:
    return MARKET_RESEARCH_PROMPT.format(repo_name=repo_name)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_prompts_market_research.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/prompts.py tests/test_prompts_market_research.py
git commit -m "feat(prompts): MARKET_RESEARCH_PROMPT + build_market_research_prompt"
```

---

## Task 8: PO prompt change — brief becomes a required input

**Files:**
- Modify: `agent/prompts.py`
- Test: `tests/test_prompts_po_with_brief.py` (new)

**Why this task:** The PO prompt must (a) render the brief unconditionally, (b) tell the model to ground every non-bug suggestion in cited URLs, (c) include `evidence_urls` in the required output schema.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_prompts_po_with_brief.py`:
```python
"""Tests for build_po_analysis_prompt with the new required `brief` input."""

import pytest

from agent.prompts import build_po_analysis_prompt


def _fake_brief(**overrides):
    class FakeBrief:
        product_category = "AI dev tools"
        competitors = [
            {"name": "Cursor", "url": "https://cursor.com", "why_relevant": "AI IDE"},
        ]
        findings = [
            {"theme": "agents",
             "observation": "competitors ship multi-agent",
             "sources": ["https://cursor.com"]},
        ]
        modality_gaps = [
            {"modality": "voice",
             "opportunity": "no voice control today",
             "sources": ["https://cursor.com"]},
        ]
        strategic_themes = [
            {"theme": "AI-native",
             "why_now": "post-GPT-5 momentum",
             "sources": ["https://cursor.com"]},
        ]
        summary = "Market is shifting to multi-modal AI dev tools."
        created_at = None
    b = FakeBrief()
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def test_po_prompt_requires_brief():
    with pytest.raises(TypeError):
        # Missing `brief` should raise — it's required.
        build_po_analysis_prompt(ux_knowledge="x", recent_suggestions=[], goal=None)  # type: ignore[call-arg]


def test_po_prompt_renders_market_context_section():
    p = build_po_analysis_prompt(
        brief=_fake_brief(), ux_knowledge="x", recent_suggestions=[], goal=None,
    )
    assert "Market context" in p
    assert "Cursor" in p
    assert "cursor.com" in p
    assert "voice" in p.lower()
    assert "AI-native" in p or "ai-native" in p.lower()


def test_po_prompt_requires_evidence_urls_in_output_schema():
    p = build_po_analysis_prompt(
        brief=_fake_brief(), ux_knowledge="x", recent_suggestions=[], goal=None,
    )
    assert "evidence_urls" in p


def test_po_prompt_states_grounding_rule():
    p = build_po_analysis_prompt(
        brief=_fake_brief(), ux_knowledge="x", recent_suggestions=[], goal=None,
    )
    lower = p.lower()
    assert "must be motivated" in lower or "must be grounded" in lower or "drop suggestions" in lower
```

- [ ] **Step 2: Run tests to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_prompts_po_with_brief.py -v
```

Expected: FAIL — current `build_po_analysis_prompt` has no `brief` parameter.

- [ ] **Step 3: Update `PO_ANALYSIS_PROMPT`**

In `agent/prompts.py`, replace the existing `PO_ANALYSIS_PROMPT` body with:
```python
PO_ANALYSIS_PROMPT = """\
You are a Product Owner analyzing a codebase to identify UX improvements and feature gaps.
{goal_section}
## Market context (brief from the market researcher)

### Product category
{brief_product_category}

### Competitors and what they offer
{brief_competitors}

### Themes from the market
{brief_findings}

### Modality opportunities (voice, vision, AI-native, multi-modal)
{brief_modality_gaps}

### Strategic / why-now themes
{brief_strategic_themes}

### Brief summary
{brief_summary}

## Your accumulated knowledge about this product
{ux_knowledge}

## Recently suggested (do NOT re-suggest these)
{recent_suggestions}

## Instructions

1. Explore the user-facing code: routes, pages, components, templates, API endpoints.
2. Map out user journeys: what can users do? What's the flow from start to finish?
3. Identify 3-5 actionable improvements. For EACH suggestion:
   - It must be motivated by at least one item from the Market context above
     OR be an obvious bug / UX defect (in which case category="bug" and
     evidence_urls=[]).
   - Prefer suggestions that introduce a new capability or modality the repo
     currently lacks over suggestions that polish what already exists.
   - Cite the URLs your suggestion draws from in `evidence_urls`.
   - Drop suggestions you cannot ground in either market evidence or a
     visible repo defect — those are the "button-sized" suggestions we
     don't want.
   {goal_directive}
4. Update your knowledge summary with what you learned about the product.

## Output format (STRICT JSON — no markdown fences, no commentary)
{{
  "suggestions": [
    {{
      "title": "Short actionable title",
      "description": "Implementation-ready description with specific files/components to change",
      "rationale": "Why this matters for users",
      "category": "ux_gap|feature|improvement|bug",
      "priority": 1,
      "evidence_urls": [
        {{"url": "https://...", "title": "Source title", "excerpt": "what was said"}}
      ]
    }}
  ],
  "ux_knowledge_update": "Updated summary of product understanding..."
}}

Priority: 1=critical, 2=high, 3=medium, 4=low, 5=nice-to-have.
Output ONLY the JSON object. No other text.
"""
```

- [ ] **Step 4: Update `build_po_analysis_prompt`**

Replace the existing function with:
```python
def build_po_analysis_prompt(
    *,
    brief,  # MarketBrief — required (kw-only to prevent positional confusion)
    ux_knowledge: str | None = None,
    recent_suggestions: list[str] | None = None,
    goal: str | None = None,
) -> str:
    knowledge = ux_knowledge or "No prior knowledge — this is the first analysis."
    suggestions = "\n".join(f"- {s}" for s in (recent_suggestions or []))
    if not suggestions:
        suggestions = "None yet — this is the first analysis."

    goal_clean = (goal or "").strip()
    if goal_clean:
        goal_section = (
            "\n## Goal (the product owner's objective for this analysis)\n"
            f"{goal_clean}\n\n"
            "Every suggestion you propose must move the product toward this "
            "goal. Drop any otherwise-good ideas that don't.\n"
        )
        goal_directive = (
            "- **Goal alignment** — items that directly serve the stated goal "
            "above take priority over generic improvements."
        )
    else:
        goal_section = ""
        goal_directive = ""

    def _bullet_list(items, fmt):
        if not items:
            return "(none)"
        return "\n".join(fmt(i) for i in items)

    competitors = _bullet_list(
        brief.competitors or [],
        lambda c: f"- **{c.get('name','?')}** ({c.get('url','')}) — {c.get('why_relevant','')}",
    )
    findings = _bullet_list(
        brief.findings or [],
        lambda f: f"- **{f.get('theme','?')}**: {f.get('observation','')}  "
                  f"[sources: {', '.join(f.get('sources', []))}]",
    )
    modality_gaps = _bullet_list(
        brief.modality_gaps or [],
        lambda m: f"- **{m.get('modality','?')}**: {m.get('opportunity','')}  "
                  f"[sources: {', '.join(m.get('sources', []))}]",
    )
    strategic_themes = _bullet_list(
        brief.strategic_themes or [],
        lambda t: f"- **{t.get('theme','?')}**: {t.get('why_now','')}  "
                  f"[sources: {', '.join(t.get('sources', []))}]",
    )

    return PO_ANALYSIS_PROMPT.format(
        goal_section=goal_section,
        goal_directive=goal_directive,
        ux_knowledge=knowledge,
        recent_suggestions=suggestions,
        brief_product_category=brief.product_category or "(unknown)",
        brief_competitors=competitors,
        brief_findings=findings,
        brief_modality_gaps=modality_gaps,
        brief_strategic_themes=strategic_themes,
        brief_summary=brief.summary or "(no summary)",
    )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_prompts_po_with_brief.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/prompts.py tests/test_prompts_po_with_brief.py
git commit -m "feat(prompts): PO prompt requires a brief; renders market context as data"
```

---

## Task 9: `agent/market_researcher.py` — the researcher module

**Files:**
- Create: `agent/market_researcher.py`
- Create: `tests/test_market_researcher.py`

**Why this task:** Single async helper `run_market_research` that runs the agent with web tools, persists a `MarketBrief`, and emits events. Mirrors `agent/po_analyzer.handle_po_analysis` shape but is **not** its own cron loop.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_market_researcher.py`. Look at `tests/test_architecture_mode.py` for the existing patterns of stubbing `create_agent` and `clone_repo`. The test file template:

```python
"""Tests for agent/market_researcher.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from shared.database import async_session
from shared.models import FreeformConfig, MarketBrief


@pytest.fixture
def fake_agent_output():
    """Canned researcher output."""
    return json.dumps({
        "product_category": "AI dev tools",
        "competitors": [
            {"name": "Cursor", "url": "https://cursor.com", "why_relevant": "AI IDE"},
        ],
        "findings": [
            {"theme": "agents", "observation": "multi-agent rising",
             "sources": ["https://cursor.com/blog/agents"]},
        ],
        "modality_gaps": [
            {"modality": "voice", "opportunity": "no voice yet",
             "sources": ["https://cursor.com"]},
        ],
        "strategic_themes": [
            {"theme": "AI-native", "why_now": "post-GPT-5",
             "sources": ["https://cursor.com"]},
        ],
        "summary": "Multi-agent and voice are rising in AI dev tools."
    })


def _fake_workspace_state(fetched_urls):
    """Build a stand-in for the agent's WorkspaceState."""
    from agent.context.workspace_state import WorkspaceState
    state = WorkspaceState()
    for url in fetched_urls:
        state.process_tool_call("fetch_url", {"url": url})
    return state


@pytest.mark.asyncio
async def test_researcher_writes_brief_with_raw_sources(
    test_org_id, test_repo, monkeypatch, fake_agent_output
):
    from agent import market_researcher

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(
        return_value=MagicMock(
            output=fake_agent_output,
            workspace_state=_fake_workspace_state(
                ["https://cursor.com", "https://cursor.com/blog/agents"]
            ),
            turns=12,
        )
    )

    monkeypatch.setattr(
        market_researcher, "create_agent", lambda *a, **kw: fake_agent,
    )
    monkeypatch.setattr(
        market_researcher,
        "clone_repo",
        AsyncMock(return_value="/tmp/fake-workspace"),
    )

    async with async_session() as session:
        cfg = (
            await session.execute(
                select(FreeformConfig).where(FreeformConfig.repo_id == test_repo.id)
            )
        ).scalar_one_or_none()
        if cfg is None:
            cfg = FreeformConfig(
                repo_id=test_repo.id, organization_id=test_org_id, enabled=True,
            )
            session.add(cfg)
            await session.commit()

        brief = await market_researcher.run_market_research(session, cfg, test_repo)

    assert brief is not None
    assert brief.product_category == "AI dev tools"
    assert len(brief.raw_sources) == 2
    urls = {s["url"] for s in brief.raw_sources}
    assert urls == {"https://cursor.com", "https://cursor.com/blog/agents"}
    assert brief.partial is False
    assert brief.agent_turns == 12


@pytest.mark.asyncio
async def test_researcher_returns_none_when_output_unparseable(
    test_org_id, test_repo, monkeypatch
):
    from agent import market_researcher

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(
        return_value=MagicMock(
            output="this is not json",
            workspace_state=_fake_workspace_state([]),
            turns=3,
        )
    )
    monkeypatch.setattr(
        market_researcher, "create_agent", lambda *a, **kw: fake_agent
    )
    monkeypatch.setattr(
        market_researcher,
        "clone_repo",
        AsyncMock(return_value="/tmp/fake-workspace"),
    )

    async with async_session() as session:
        cfg = FreeformConfig(
            repo_id=test_repo.id, organization_id=test_org_id, enabled=True,
        )
        session.add(cfg)
        await session.commit()

        brief = await market_researcher.run_market_research(session, cfg, test_repo)

    assert brief is None


@pytest.mark.asyncio
async def test_researcher_marks_partial_when_output_is_empty_json(
    test_org_id, test_repo, monkeypatch
):
    from agent import market_researcher

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(
        return_value=MagicMock(
            output="{}",
            workspace_state=_fake_workspace_state([]),
            turns=20,
        )
    )
    monkeypatch.setattr(
        market_researcher, "create_agent", lambda *a, **kw: fake_agent
    )
    monkeypatch.setattr(
        market_researcher,
        "clone_repo",
        AsyncMock(return_value="/tmp/fake-workspace"),
    )

    async with async_session() as session:
        cfg = FreeformConfig(
            repo_id=test_repo.id, organization_id=test_org_id, enabled=True,
        )
        session.add(cfg)
        await session.commit()

        brief = await market_researcher.run_market_research(session, cfg, test_repo)

    assert brief is not None
    assert brief.partial is True
```

(Replace `test_org_id` / `test_repo` with whatever fixtures the existing tests use. See `tests/test_architecture_mode.py` for reference.)

- [ ] **Step 2: Run test to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_market_researcher.py -v
```

Expected: FAIL — `agent.market_researcher` module does not exist.

- [ ] **Step 3: Create `agent/market_researcher.py`**

```python
"""Market-research analyzer — runs before the PO to ground its suggestions.

Single helper `run_market_research(session, config, repo)` that runs an
agent with web tools (Brave Search + fetch_url), parses the result into a
MarketBrief row, and returns it. Called inline by the PO loop in
`agent/po_analyzer.py` when the latest brief is stale.

Not its own cron. Failures are non-fatal — the PO loop decides what to
do with `None` (fall back to a prior brief, or skip the cycle).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent.llm.structured import parse_json_response
from agent.prompts import build_market_research_prompt
from agent.workspace import clone_repo
from shared.events import (
    market_research_completed,
    market_research_failed,
    market_research_started,
    publish,
)
from shared.models import MarketBrief

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from shared.models import FreeformConfig, Repo

log = logging.getLogger(__name__)

MAX_TURNS = 20


async def run_market_research(
    session: AsyncSession,
    config: FreeformConfig,
    repo: Repo,
) -> MarketBrief | None:
    """Run the researcher agent, persist a MarketBrief, return it.

    Returns None on failure (web tools unavailable, unparseable output).
    Updates ``config.last_market_research_at`` on success.
    """
    from agent.lifecycle.factory import create_agent

    ws_name = f"market-{repo.name.replace('/', '-')}"
    try:
        workspace = await clone_repo(
            repo.url,
            0,
            config.dev_branch or repo.default_branch,
            workspace_name=ws_name,
        )
    except Exception:
        log.exception("market researcher: clone failed for repo=%s", repo.name)
        await publish(market_research_failed(repo_name=repo.name, reason="clone failed"))
        return None

    prompt = build_market_research_prompt(repo_name=repo.name)
    await publish(market_research_started(repo_name=repo.name))

    try:
        agent = create_agent(
            workspace,
            readonly=True,
            with_web=True,
            max_turns=MAX_TURNS,
            task_description=(
                f"Market research for {repo.name}: produce a sourced brief "
                "for the PO."
            ),
            repo_name=repo.name,
        )
        result = await agent.run(prompt)
    except Exception:
        log.exception("market researcher: agent run failed for repo=%s", repo.name)
        await publish(
            market_research_failed(repo_name=repo.name, reason="agent run failed")
        )
        return None

    data = parse_json_response(result.output)
    if data is None:
        log.warning("market researcher: unparseable output for repo=%s", repo.name)
        await publish(
            market_research_failed(
                repo_name=repo.name, reason="unparseable output",
            )
        )
        return None

    fetched_urls = _raw_sources_from_state(result)

    # Empty / minimal payload → still persist, mark partial so PO knows
    has_real_content = any(
        data.get(k) for k in ("competitors", "findings", "modality_gaps", "strategic_themes")
    )

    brief = MarketBrief(
        repo_id=repo.id,
        organization_id=config.organization_id,
        product_category=data.get("product_category"),
        competitors=data.get("competitors", []),
        findings=data.get("findings", []),
        modality_gaps=data.get("modality_gaps", []),
        strategic_themes=data.get("strategic_themes", []),
        summary=data.get("summary", "") or "",
        raw_sources=fetched_urls,
        partial=not has_real_content,
        agent_turns=getattr(result, "turns", 0),
    )
    session.add(brief)
    config.last_market_research_at = datetime.now(UTC)
    await session.flush()

    await publish(
        market_research_completed(
            repo_name=repo.name,
            brief_id=brief.id,
            n_competitors=len(brief.competitors or []),
            n_findings=len(brief.findings or []),
            partial=brief.partial,
        )
    )
    log.info(
        "market researcher: brief written repo=%s id=%s partial=%s",
        repo.name, brief.id, brief.partial,
    )
    return brief


def _raw_sources_from_state(agent_result) -> list[dict]:
    """Extract URL fetch telemetry from the agent's WorkspaceState.

    The agent could mention URLs in its output text, but we trust tool-call
    telemetry instead — it's deterministic and the agent can't forget to log it.
    """
    state = getattr(agent_result, "workspace_state", None)
    if state is None:
        return []
    sources: list[dict] = []
    seen: set[str] = set()
    for entry in getattr(state, "url_fetches", []) or []:
        url = entry.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({
            "url": url,
            "title": "",  # filled by future enhancement (title-extract during fetch)
            "fetched_at": datetime.now(UTC).isoformat(),
        })
    return sources
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_market_researcher.py -v
```

Expected: all PASS. If any test fails because the existing AgentLoop result object doesn't expose `workspace_state` as an attribute (it might expose it via `agent.workspace_state` instead of `result.workspace_state`), inspect `agent/loop.py` to confirm the right access path and adjust both the test fixture and `_raw_sources_from_state` accordingly.

- [ ] **Step 5: Commit**

```bash
git add agent/market_researcher.py tests/test_market_researcher.py
git commit -m "feat(market_researcher): inline researcher producing MarketBrief rows"
```

---

## Task 10: PO chain wiring + post-parse filter

**Files:**
- Modify: `agent/po_analyzer.py`
- Test: `tests/test_po_with_market_research.py` (new)

**Why this task:** This is where everything snaps together. `_check_and_analyze` decides "fresh brief or run researcher?", `handle_po_analysis` takes the required `brief`, and the post-parse filter drops ungrounded non-bug suggestions.

- [ ] **Step 1: Write the failing integration tests**

Create `tests/test_po_with_market_research.py`. Use the same fixture patterns the existing PO tests use:
```python
"""Integration tests for the researcher → PO chain."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from shared.database import async_session
from shared.models import (
    FreeformConfig,
    MarketBrief,
    Suggestion,
)


def _po_output_with_two_grounded_suggestions():
    return json.dumps({
        "suggestions": [
            {"title": "Add voice", "description": "...", "rationale": "...",
             "category": "feature", "priority": 2,
             "evidence_urls": [{"url": "https://x.example", "title": "X", "excerpt": "voice"}]},
            {"title": "Fix login crash", "description": "...", "rationale": "...",
             "category": "bug", "priority": 1, "evidence_urls": []},
        ],
        "ux_knowledge_update": "...",
    })


def _wire_chain_mocks(monkeypatch, *, brief_data, po_output):
    """Stub clone_repo + create_agent for both researcher and PO."""
    from agent import market_researcher, po_analyzer
    from agent.context.workspace_state import WorkspaceState

    state = WorkspaceState()
    state.process_tool_call("fetch_url", {"url": "https://x.example"})

    researcher_agent = MagicMock()
    researcher_agent.run = AsyncMock(
        return_value=MagicMock(
            output=json.dumps(brief_data),
            workspace_state=state,
            turns=8,
        )
    )

    po_agent = MagicMock()
    po_agent.run = AsyncMock(
        return_value=MagicMock(
            output=po_output, workspace_state=WorkspaceState(), turns=10,
        )
    )

    call_counter = {"researcher": 0, "po": 0}

    def fake_create_agent(*args, **kwargs):
        if kwargs.get("with_web"):
            call_counter["researcher"] += 1
            return researcher_agent
        call_counter["po"] += 1
        return po_agent

    monkeypatch.setattr(market_researcher, "create_agent", fake_create_agent)
    monkeypatch.setattr(po_analyzer, "create_agent", fake_create_agent)
    monkeypatch.setattr(
        market_researcher,
        "clone_repo",
        AsyncMock(return_value="/tmp/fake-workspace"),
    )
    monkeypatch.setattr(
        po_analyzer, "clone_repo", AsyncMock(return_value="/tmp/fake-workspace"),
    )
    return call_counter


@pytest.fixture
def fixture_brief_data():
    return {
        "product_category": "AI dev tools",
        "competitors": [{"name": "X", "url": "https://x.example", "why_relevant": "y"}],
        "findings": [{"theme": "agents", "observation": "z",
                      "sources": ["https://x.example"]}],
        "modality_gaps": [],
        "strategic_themes": [],
        "summary": "...",
    }


@pytest.mark.asyncio
async def test_stale_brief_triggers_researcher_then_po(
    test_org_id, test_repo, monkeypatch, fixture_brief_data
):
    from agent.po_analyzer import _check_and_analyze

    counter = _wire_chain_mocks(
        monkeypatch,
        brief_data=fixture_brief_data,
        po_output=_po_output_with_two_grounded_suggestions(),
    )

    async with async_session() as session:
        cfg = FreeformConfig(
            repo_id=test_repo.id,
            organization_id=test_org_id,
            enabled=True,
            analysis_cron="* * * * *",  # always due
            last_analysis_at=None,
        )
        session.add(cfg)
        await session.commit()

        await _check_and_analyze(session)

        # Researcher ran exactly once, PO ran exactly once
        assert counter["researcher"] == 1
        assert counter["po"] == 1

        brief = (
            await session.execute(select(MarketBrief).where(MarketBrief.repo_id == test_repo.id))
        ).scalar_one()
        assert brief.product_category == "AI dev tools"

        suggestions = (
            await session.execute(select(Suggestion).where(Suggestion.repo_id == test_repo.id))
        ).scalars().all()
        # Both fixture suggestions should land (1 feature with evidence + 1 bug)
        assert len(suggestions) == 2
        for s in suggestions:
            assert s.brief_id == brief.id


@pytest.mark.asyncio
async def test_fresh_brief_skips_researcher(
    test_org_id, test_repo, monkeypatch, fixture_brief_data
):
    from agent.po_analyzer import _check_and_analyze

    counter = _wire_chain_mocks(
        monkeypatch,
        brief_data=fixture_brief_data,
        po_output=_po_output_with_two_grounded_suggestions(),
    )

    async with async_session() as session:
        cfg = FreeformConfig(
            repo_id=test_repo.id, organization_id=test_org_id,
            enabled=True, analysis_cron="* * * * *", last_analysis_at=None,
            market_brief_max_age_days=7,
        )
        session.add(cfg)
        # Existing fresh brief (1 day old)
        brief = MarketBrief(
            repo_id=test_repo.id,
            organization_id=test_org_id,
            product_category="prior",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        session.add(brief)
        await session.commit()

        await _check_and_analyze(session)

        # Researcher did NOT run; PO did
        assert counter["researcher"] == 0
        assert counter["po"] == 1


@pytest.mark.asyncio
async def test_researcher_failure_with_prior_brief_uses_prior(
    test_org_id, test_repo, monkeypatch
):
    from agent import market_researcher, po_analyzer
    from agent.po_analyzer import _check_and_analyze
    from agent.context.workspace_state import WorkspaceState

    # Researcher returns unparseable → run_market_research returns None
    researcher_agent = MagicMock()
    researcher_agent.run = AsyncMock(
        return_value=MagicMock(
            output="not json", workspace_state=WorkspaceState(), turns=2,
        )
    )
    po_agent = MagicMock()
    po_agent.run = AsyncMock(
        return_value=MagicMock(
            output=_po_output_with_two_grounded_suggestions(),
            workspace_state=WorkspaceState(),
            turns=5,
        )
    )

    def fake_create_agent(*args, **kwargs):
        return researcher_agent if kwargs.get("with_web") else po_agent

    monkeypatch.setattr(market_researcher, "create_agent", fake_create_agent)
    monkeypatch.setattr(po_analyzer, "create_agent", fake_create_agent)
    monkeypatch.setattr(market_researcher, "clone_repo", AsyncMock(return_value="/tmp/x"))
    monkeypatch.setattr(po_analyzer, "clone_repo", AsyncMock(return_value="/tmp/x"))

    async with async_session() as session:
        cfg = FreeformConfig(
            repo_id=test_repo.id, organization_id=test_org_id,
            enabled=True, analysis_cron="* * * * *", last_analysis_at=None,
            market_brief_max_age_days=7,
        )
        session.add(cfg)
        # Stale brief (older than 7 days) — researcher will try, will fail,
        # PO should still run with this stale brief.
        prior = MarketBrief(
            repo_id=test_repo.id, organization_id=test_org_id,
            product_category="stale prior",
            created_at=datetime.now(UTC) - timedelta(days=30),
        )
        session.add(prior)
        await session.commit()

        await _check_and_analyze(session)

        suggestions = (
            await session.execute(select(Suggestion).where(Suggestion.repo_id == test_repo.id))
        ).scalars().all()
        assert len(suggestions) == 2  # PO ran with the stale prior
        assert suggestions[0].brief_id == prior.id


@pytest.mark.asyncio
async def test_researcher_failure_no_prior_skips_cycle(
    test_org_id, test_repo, monkeypatch
):
    from agent import market_researcher, po_analyzer
    from agent.context.workspace_state import WorkspaceState
    from agent.po_analyzer import _check_and_analyze

    researcher_agent = MagicMock()
    researcher_agent.run = AsyncMock(
        return_value=MagicMock(
            output="not json", workspace_state=WorkspaceState(), turns=2,
        )
    )

    po_called = {"v": False}

    def fake_create_agent(*args, **kwargs):
        if kwargs.get("with_web"):
            return researcher_agent
        po_called["v"] = True
        raise AssertionError("PO must not be called when no brief is available")

    monkeypatch.setattr(market_researcher, "create_agent", fake_create_agent)
    monkeypatch.setattr(po_analyzer, "create_agent", fake_create_agent)
    monkeypatch.setattr(market_researcher, "clone_repo", AsyncMock(return_value="/tmp/x"))
    monkeypatch.setattr(po_analyzer, "clone_repo", AsyncMock(return_value="/tmp/x"))

    async with async_session() as session:
        cfg = FreeformConfig(
            repo_id=test_repo.id, organization_id=test_org_id,
            enabled=True, analysis_cron="* * * * *", last_analysis_at=None,
            market_brief_max_age_days=7,
        )
        session.add(cfg)
        await session.commit()

        await _check_and_analyze(session)

        assert po_called["v"] is False
        # last_analysis_at should have advanced (back-off)
        await session.refresh(cfg)
        assert cfg.last_analysis_at is not None
```

- [ ] **Step 2: Run tests to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_po_with_market_research.py -v
```

Expected: most fail — current `_check_and_analyze` does not call the researcher and `handle_po_analysis` does not require a brief.

- [ ] **Step 3: Modify `agent/po_analyzer.py`**

In `_check_and_analyze`, replace the body of the `for config in configs` loop with the chained version. Replace existing:
```python
        if _is_due(config, now):
            log.info(f"PO analysis due for repo_id={config.repo_id}")
            try:
                await handle_po_analysis(session, config)
                config.last_analysis_at = now
                await session.commit()
            except Exception:
                ...
```

with:
```python
        if _is_due(config, now):
            log.info(f"PO analysis due for repo_id={config.repo_id}")
            try:
                brief = await _ensure_brief(session, config)
                if brief is None:
                    await publish(
                        po_analysis_failed(
                            repo_name=(config.repo.name if config.repo else "?"),
                            reason="no brief",
                        )
                    )
                    config.last_analysis_at = now  # back-off
                    await session.commit()
                    continue
                await handle_po_analysis(session, config, brief=brief)
                config.last_analysis_at = now
                await session.commit()
            except Exception:
                log.exception(f"PO analysis failed for repo_id={config.repo_id}")
                if _FAILURE_BACKOFF_NOW:
                    config.last_analysis_at = now
                    await session.commit()
```

Add helper `_ensure_brief` to the module:
```python
async def _ensure_brief(session: AsyncSession, config: FreeformConfig) -> MarketBrief | None:
    """Return a fresh MarketBrief for `config.repo_id`.

    Returns the latest existing brief if it's within `market_brief_max_age_days`.
    Otherwise runs the researcher. If the researcher fails, falls back to the
    most recent prior brief (even if stale). Returns None if nothing exists.
    """
    from agent.market_researcher import run_market_research

    latest = (
        await session.execute(
            select(MarketBrief)
            .where(MarketBrief.repo_id == config.repo_id)
            .order_by(MarketBrief.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if _brief_is_fresh(latest, now, config.market_brief_max_age_days):
        return latest

    repo = (
        await session.execute(select(Repo).where(Repo.id == config.repo_id))
    ).scalar_one_or_none()
    if repo is None:
        return latest  # repo gone — return whatever we have

    new_brief = await run_market_research(session, config, repo)
    if new_brief is not None:
        return new_brief
    return latest  # researcher failed; fall back to whatever we had
```

Modify `handle_po_analysis` signature and apply the filter. Find the existing function and change:
```python
async def handle_po_analysis(session: AsyncSession, config: FreeformConfig) -> None:
```
to:
```python
async def handle_po_analysis(
    session: AsyncSession, config: FreeformConfig, *, brief: MarketBrief,
) -> None:
```

In its body, replace:
```python
    prompt = build_po_analysis_prompt(
        ux_knowledge=config.ux_knowledge,
        recent_suggestions=recent_titles,
        goal=config.po_goal,
    )
```
with:
```python
    prompt = build_po_analysis_prompt(
        brief=brief,
        ux_knowledge=config.ux_knowledge,
        recent_suggestions=recent_titles,
        goal=config.po_goal,
    )
```

And replace the existing `for s in new_suggestions:` loop with the version that filters + stamps brief_id:
```python
    filtered, dropped = _filter_grounded(new_suggestions)
    if dropped:
        log.info(
            "PO filtered %d ungrounded suggestion(s) for repo='%s'",
            dropped, repo.name,
        )

    for s in filtered:
        suggestion = Suggestion(
            repo_id=config.repo_id,
            organization_id=config.organization_id,
            title=s.get("title", "Untitled"),
            description=s.get("description", ""),
            rationale=s.get("rationale", ""),
            category=s.get("category", "improvement"),
            priority=s.get("priority", 3),
            status=SuggestionStatus.PENDING,
            evidence_urls=s.get("evidence_urls", []),
            brief_id=brief.id,
        )
        session.add(suggestion)
        await remember_priority_suggestion(
            repo_name=repo.name,
            title=suggestion.title,
            rationale=suggestion.rationale,
            priority=suggestion.priority,
            category=f"PO suggestion / {suggestion.category}",
            source="po-analyzer",
        )
```

Add the filter helper at module scope:
```python
def _filter_grounded(suggestions: list[dict]) -> tuple[list[dict], int]:
    """Drop non-bug suggestions with no evidence URLs.

    Returns (kept, dropped_count). This is the enforcement mechanism for the
    grounding rule — the prompt asks, the filter ensures.
    """
    kept: list[dict] = []
    dropped = 0
    for s in suggestions:
        category = s.get("category", "")
        evidence = s.get("evidence_urls") or []
        if category == "bug" or evidence:
            kept.append(s)
        else:
            dropped += 1
    return kept, dropped
```

Add the necessary imports at the top of `agent/po_analyzer.py`:
```python
from shared.models import MarketBrief, Repo  # if not already imported
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_po_with_market_research.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/po_analyzer.py tests/test_po_with_market_research.py
git commit -m "feat(po_analyzer): chain researcher → PO, filter ungrounded suggestions"
```

---

## Task 11: Regression test — PO drops ungrounded suggestions

**Files:**
- Create: `tests/test_po_drops_ungrounded_suggestions.py`

**Why this task:** The single load-bearing test that proves the "no button-sized suggestions" promise. Even if the LLM ignores the prompt, the post-parse filter catches it. This test must not break, ever.

- [ ] **Step 1: Write the test**

Create `tests/test_po_drops_ungrounded_suggestions.py`:
```python
"""Regression test: PO drops non-bug suggestions with empty evidence_urls.

This is the load-bearing test for the bigger-PO/market-research overhaul.
The PO prompt asks the model to drop ungrounded suggestions, but the model
may not always obey. The post-parse filter in agent/po_analyzer.py is the
enforcement mechanism. If this test ever fails, the filter has regressed.
"""

from __future__ import annotations

from agent.po_analyzer import _filter_grounded


def test_filter_keeps_grounded_feature_suggestion():
    kept, dropped = _filter_grounded([
        {"title": "Add voice", "category": "feature",
         "evidence_urls": [{"url": "https://x.example", "title": "X", "excerpt": "v"}]},
    ])
    assert dropped == 0
    assert len(kept) == 1


def test_filter_keeps_bug_with_empty_evidence():
    kept, dropped = _filter_grounded([
        {"title": "Fix crash", "category": "bug", "evidence_urls": []},
    ])
    assert dropped == 0
    assert len(kept) == 1


def test_filter_drops_ungrounded_feature():
    """The 'add a button' suggestion type — this is the bug we are fixing."""
    kept, dropped = _filter_grounded([
        {"title": "Add a small icon", "category": "ux_gap", "evidence_urls": []},
    ])
    assert dropped == 1
    assert kept == []


def test_filter_drops_ungrounded_improvement():
    kept, dropped = _filter_grounded([
        {"title": "Generic polish", "category": "improvement", "evidence_urls": []},
    ])
    assert dropped == 1


def test_filter_mixed_input():
    kept, dropped = _filter_grounded([
        {"title": "Add voice", "category": "feature",
         "evidence_urls": [{"url": "https://x", "title": "", "excerpt": ""}]},
        {"title": "Fix login crash", "category": "bug", "evidence_urls": []},
        {"title": "Reorder buttons", "category": "ux_gap", "evidence_urls": []},
        {"title": "Add multi-modal input", "category": "feature",
         "evidence_urls": [{"url": "https://y", "title": "", "excerpt": ""}]},
    ])
    assert dropped == 1
    titles = {s["title"] for s in kept}
    assert titles == {"Add voice", "Fix login crash", "Add multi-modal input"}
```

- [ ] **Step 2: Run the test**

```bash
.venv/bin/python3 -m pytest tests/test_po_drops_ungrounded_suggestions.py -v
```

Expected: all PASS (the filter was already implemented in Task 10).

- [ ] **Step 3: Commit**

```bash
git add tests/test_po_drops_ungrounded_suggestions.py
git commit -m "test(po_analyzer): regression test for ungrounded-suggestion filter"
```

---

## Task 12: API endpoint — `GET /api/repos/{id}/market-brief/latest`

**Files:**
- Modify: `orchestrator/router.py`
- Test: `tests/test_market_brief_endpoint.py` (new)

- [ ] **Step 1: Inspect the existing router for patterns**

```bash
grep -n "Suggestion\|/repos/" orchestrator/router.py | head -30
```

Note the existing async endpoint pattern for fetching repo-scoped data (auth dependency, async session, JSON-serialized response). Reuse it.

- [ ] **Step 2: Write the failing test**

Create `tests/test_market_brief_endpoint.py`:
```python
"""Tests for GET /api/repos/{repo_id}/market-brief/latest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import async_session
from shared.models import MarketBrief


@pytest.mark.asyncio
async def test_market_brief_endpoint_returns_404_when_none(
    test_client: AsyncClient, test_repo, authed_headers
):
    resp = await test_client.get(
        f"/api/repos/{test_repo.id}/market-brief/latest", headers=authed_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_market_brief_endpoint_returns_latest(
    test_client: AsyncClient, test_org_id, test_repo, authed_headers,
):
    async with async_session() as session:
        older = MarketBrief(
            repo_id=test_repo.id, organization_id=test_org_id,
            product_category="OLD",
            created_at=datetime.now(UTC) - timedelta(days=10),
        )
        newer = MarketBrief(
            repo_id=test_repo.id, organization_id=test_org_id,
            product_category="NEW",
        )
        session.add_all([older, newer])
        await session.commit()
        newer_id = newer.id

    resp = await test_client.get(
        f"/api/repos/{test_repo.id}/market-brief/latest", headers=authed_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == newer_id
    assert body["product_category"] == "NEW"
```

(Use whatever `test_client` / `authed_headers` fixtures exist in `tests/conftest.py`; check `tests/test_search_endpoint.py` for a working pattern.)

- [ ] **Step 3: Run test to verify failure**

```bash
.venv/bin/python3 -m pytest tests/test_market_brief_endpoint.py -v
```

Expected: FAIL — endpoint does not exist.

- [ ] **Step 4: Add the endpoint in `orchestrator/router.py`**

Find an existing `/api/repos/...` GET endpoint to use as a template. Add:
```python
@router.get("/api/repos/{repo_id}/market-brief/latest")
async def get_latest_market_brief(
    repo_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db_session),
):
    """Return the most recent MarketBrief for a repo, or 404 if none."""
    from shared.models import MarketBrief

    brief = (
        await session.execute(
            select(MarketBrief)
            .where(MarketBrief.repo_id == repo_id)
            .where(MarketBrief.organization_id == user.organization_id)
            .order_by(MarketBrief.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if brief is None:
        raise HTTPException(status_code=404, detail="No brief yet")
    return {
        "id": brief.id,
        "repo_id": brief.repo_id,
        "created_at": brief.created_at.isoformat(),
        "product_category": brief.product_category,
        "competitors": brief.competitors,
        "findings": brief.findings,
        "modality_gaps": brief.modality_gaps,
        "strategic_themes": brief.strategic_themes,
        "summary": brief.summary,
        "partial": brief.partial,
    }
```

(Substitute the exact names of the auth dependency / DB session dependency used by other endpoints in `orchestrator/router.py`. Do not invent new ones.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_market_brief_endpoint.py -v
.venv/bin/python3 -m pytest tests/ -q
```

Expected: new tests PASS, all pre-existing tests still PASS.

- [ ] **Step 6: Regenerate TS types so web-next has the new shape**

```bash
python3.12 scripts/gen_ts_types.py
```

(If this script generates types from Pydantic — currently the endpoint returns a plain dict. If the script requires a Pydantic schema, define `MarketBriefResponse` in `shared/types.py` and have the endpoint return that. See the project CLAUDE.md note about TS type regen.)

- [ ] **Step 7: Commit**

```bash
git add orchestrator/router.py tests/test_market_brief_endpoint.py shared/types.py web-next/types
git commit -m "feat(api): GET /api/repos/:id/market-brief/latest"
```

---

## Task 13: UI — Suggestion card evidence footer + brief modal

**Files:**
- Modify: `web-next/app/(app)/suggestions/page.tsx`
- Create: `web-next/lib/market-brief.ts`
- Create: `web-next/components/market-brief/market-brief-modal.tsx`

**Why this task:** Two changes give the feature its visible payoff: source links on each grounded suggestion, and a one-click modal showing the brief.

- [ ] **Step 1: Read the suggestions page to find the rendering site**

```bash
sed -n '180,260p' web-next/app/\(app\)/suggestions/page.tsx
```

Locate where `suggestion.rationale` is rendered (around line 236 per earlier inspection). The new "Backed by" footer goes just after that block.

- [ ] **Step 2: Create the TanStack Query hook + fetcher**

Create `web-next/lib/market-brief.ts`:
```ts
import { useQuery } from "@tanstack/react-query";

export type MarketBrief = {
  id: number;
  repo_id: number;
  created_at: string;
  product_category: string | null;
  competitors: Array<{ name: string; url: string; why_relevant: string }>;
  findings: Array<{ theme: string; observation: string; sources: string[] }>;
  modality_gaps: Array<{ modality: string; opportunity: string; sources: string[] }>;
  strategic_themes: Array<{ theme: string; why_now: string; sources: string[] }>;
  summary: string;
  partial: boolean;
};

export function useLatestMarketBrief(repoId: number, enabled = true) {
  return useQuery({
    queryKey: ["market-brief", repoId],
    enabled: enabled && Number.isFinite(repoId),
    queryFn: async (): Promise<MarketBrief | null> => {
      const resp = await fetch(`/api/repos/${repoId}/market-brief/latest`);
      if (resp.status === 404) return null;
      if (!resp.ok) throw new Error(`brief fetch failed: ${resp.status}`);
      return resp.json();
    },
  });
}
```

- [ ] **Step 3: Create the modal component**

Create `web-next/components/market-brief/market-brief-modal.tsx`:
```tsx
"use client";

import { useLatestMarketBrief } from "@/lib/market-brief";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function MarketBriefModal({
  repoId,
  open,
  onOpenChange,
}: {
  repoId: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { data: brief, isLoading } = useLatestMarketBrief(repoId, open);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Market brief</DialogTitle>
        </DialogHeader>

        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : !brief ? (
          <p className="text-sm text-muted-foreground">
            No brief yet — runs on the next PO analysis cycle.
          </p>
        ) : (
          <div className="space-y-6 text-sm">
            <section>
              <h3 className="font-semibold">Summary</h3>
              <p className="text-muted-foreground whitespace-pre-wrap">
                {brief.summary || "(empty)"}
              </p>
            </section>

            {brief.product_category && (
              <section>
                <h3 className="font-semibold">Product category</h3>
                <p>{brief.product_category}</p>
              </section>
            )}

            <BriefList
              title="Competitors"
              items={brief.competitors.map((c) => ({
                primary: c.name,
                secondary: c.why_relevant,
                sources: [c.url],
              }))}
            />
            <BriefList
              title="Findings"
              items={brief.findings.map((f) => ({
                primary: f.theme,
                secondary: f.observation,
                sources: f.sources,
              }))}
            />
            <BriefList
              title="Modality opportunities"
              items={brief.modality_gaps.map((m) => ({
                primary: m.modality,
                secondary: m.opportunity,
                sources: m.sources,
              }))}
            />
            <BriefList
              title="Strategic themes"
              items={brief.strategic_themes.map((t) => ({
                primary: t.theme,
                secondary: t.why_now,
                sources: t.sources,
              }))}
            />

            {brief.partial && (
              <p className="text-xs text-amber-600">
                Brief is partial — the researcher hit its turn cap.
              </p>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function BriefList({
  title,
  items,
}: {
  title: string;
  items: { primary: string; secondary: string; sources: string[] }[];
}) {
  if (items.length === 0) return null;
  return (
    <section>
      <h3 className="font-semibold">{title}</h3>
      <ul className="space-y-2 mt-2">
        {items.map((item, i) => (
          <li key={i} className="border-l-2 border-muted pl-3">
            <p className="font-medium">{item.primary}</p>
            <p className="text-muted-foreground">{item.secondary}</p>
            {item.sources.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-2">
                {item.sources.map((url) => (
                  <a
                    key={url}
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-blue-600 hover:underline truncate max-w-xs"
                  >
                    {url}
                  </a>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
```

(If `@/components/ui/dialog` import path is wrong for your shadcn setup, check how other modals in `web-next/components/` import it and match that pattern. Do not invent.)

- [ ] **Step 4: Extend the suggestions page**

Open `web-next/app/(app)/suggestions/page.tsx`. Two edits:

**Edit A** — Add to the imports at the top of the file:
```tsx
import { useState } from "react";
import { MarketBriefModal } from "@/components/market-brief/market-brief-modal";
```

**Edit B** — In the page component (find where it returns the JSX header for the suggestions list), add a "View market brief" link that opens the modal for the currently-viewed repo. Implementation depends on how the page already scopes by repo — if there's a `selectedRepoId` state or URL param, reuse it:
```tsx
const [briefOpen, setBriefOpen] = useState(false);
// ... in the header next to the repo title:
{selectedRepoId && (
  <>
    <button
      type="button"
      onClick={() => setBriefOpen(true)}
      className="text-sm text-blue-600 hover:underline"
    >
      View market brief
    </button>
    <MarketBriefModal
      repoId={selectedRepoId}
      open={briefOpen}
      onOpenChange={setBriefOpen}
    />
  </>
)}
```

**Edit C** — Add the "Backed by" footer to the suggestion card. Locate the block ending around the `suggestion.rationale` paragraph (line ~239 in the current file). Immediately after it, add:
```tsx
{suggestion.evidence_urls && suggestion.evidence_urls.length > 0 && (
  <div className="mt-3 pt-3 border-t text-xs">
    <span className="text-muted-foreground mr-2">Backed by:</span>
    {suggestion.evidence_urls.slice(0, 3).map((e: any, i: number) => (
      <a
        key={i}
        href={e.url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-600 hover:underline mr-3"
        title={e.excerpt || e.url}
      >
        {e.title || new URL(e.url).hostname}
      </a>
    ))}
  </div>
)}
```

The `Suggestion` TypeScript type in `web-next/types/` (generated from Pydantic via `scripts/gen_ts_types.py`) should already include `evidence_urls` from Task 12 step 6. If it doesn't, regenerate or add it manually.

- [ ] **Step 5: Smoke-test the UI by hand**

```bash
docker compose up -d
# Open http://localhost:<port>/suggestions in a browser
# Trigger a PO run (or insert a fake Suggestion with evidence_urls and a MarketBrief in the DB to test the rendering without waiting for cron)
```

Expected:
- Suggestion cards with `evidence_urls` show a "Backed by" footer with up to 3 source links.
- The "View market brief" link opens a modal showing the brief contents.
- A repo with no brief shows "No brief yet — runs on the next PO analysis cycle."

- [ ] **Step 6: Commit**

```bash
git add web-next/app/\(app\)/suggestions/page.tsx web-next/lib/market-brief.ts web-next/components/market-brief
git commit -m "feat(web-next): suggestion evidence footer + market brief modal"
```

---

## Task 14: Final verification

- [ ] **Step 1: Run the full test suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```

Expected: all PASS. Investigate and fix any failures before claiming done.

- [ ] **Step 2: Lint**

```bash
ruff check .
ruff format --check .
```

Expected: clean output. Run `ruff check --fix .` / `ruff format .` for auto-fixable issues.

- [ ] **Step 3: Read the diff against `main` end-to-end**

```bash
git log --oneline main..HEAD
git diff main...HEAD --stat
```

Sanity-check:
- Every spec acceptance criterion has a corresponding change.
- No `print()` calls (use `structlog`).
- No leftover TODOs / debug comments.
- All `Suggestion` insertions include `organization_id` (tenant isolation invariant).
- Migration 030 is the latest, `down_revision = "029"`.

- [ ] **Step 4: Run an end-to-end smoke test against a real repo**

If you have `BRAVE_API_KEY` set in `.env`, enable freeform mode for a small test repo, wait for or manually trigger a PO cycle, and verify:
- A `market_briefs` row was written.
- The `Suggestion` rows have non-empty `evidence_urls` and non-null `brief_id`.
- The UI shows the evidence footer and the brief modal works.

If `BRAVE_API_KEY` is missing: this is fine for the unit-test suite, but the e2e smoke is skipped — note this in the PR description.

---

## Spec-coverage self-review

Cross-checked against `docs/superpowers/specs/2026-05-12-bigger-po-market-research-design.md`:

| Spec section | Covered by |
|---|---|
| Architecture chain (researcher → PO) | Tasks 9 + 10 |
| `market_briefs` table | Task 3 (migration) + Task 4 (ORM) |
| `FreeformConfig.last_market_research_at` / `market_brief_max_age_days` | Task 3 + Task 4 |
| `Suggestion.evidence_urls` / `brief_id` | Task 3 + Task 4 |
| `agent/market_researcher.py` | Task 9 |
| `handle_po_analysis` takes required brief | Task 10 |
| Post-parse filter | Task 10 + Task 11 (regression test) |
| `MARKET_RESEARCH_PROMPT` (4 phases, no package.json, citations) | Task 7 |
| `PO_ANALYSIS_PROMPT` market_context section | Task 8 |
| Three new events + taxonomy | Task 5 |
| `WorkspaceState.url_fetches` for raw_sources | Task 1 |
| Web tools in registry behind `with_web` | Task 2 |
| `GET /api/repos/:id/market-brief/latest` | Task 12 |
| Suggestion card evidence footer | Task 13 |
| "View market brief" modal | Task 13 |
| Tests: researcher unit, freshness, chain integration, regression | Tasks 6, 9, 10, 11 |

No gaps identified.

---

## Deferred (out of scope per spec)

- Brief history page in web-next.
- Per-finding "promote to task" UX.
- Per-Suggestion section-level evidence (which *part* of the brief justified each suggestion).
- Eval task case for "PO + brief" (`eval/`) — track as a follow-up.
- `market_brief_max_age_days` tunable in the freeform-config UI.
- Architecture analyzer integration (sub-project A is PO-only).
