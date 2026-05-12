"""Product Owner analyzer — periodically analyzes repos to generate improvement suggestions.

Uses the AgentLoop with readonly tools so the PO agent can explore the codebase
with grep, file_read, etc.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from croniter import croniter
from sqlalchemy import select

from agent.context.memory import remember_priority_suggestion
from agent.lifecycle.factory import create_agent
from agent.llm.structured import parse_json_response
from agent.market_researcher import run_market_research
from agent.prompts import build_po_analysis_prompt
from agent.workspace import clone_repo
from shared.database import async_session
from shared.events import (
    po_analysis_failed,
    po_analysis_started,
    po_suggestions_ready,
    publish,
)
from shared.models import FreeformConfig, MarketBrief, Repo, Suggestion, SuggestionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds

# After a failed analysis, advance last_analysis_at so the next try happens
# on the regular cron schedule rather than retrying every CHECK_INTERVAL
# seconds against a perma-broken repo.
_FAILURE_BACKOFF_NOW = True


async def run_po_analysis_loop() -> None:
    """Background loop — check freeform configs and run PO analysis when due."""
    log.info("PO analysis loop started")
    while True:
        try:
            async with async_session() as session:
                await _check_and_analyze(session)
        except Exception:
            log.exception("PO analysis loop error")
        await asyncio.sleep(CHECK_INTERVAL)


async def _check_and_analyze(session: AsyncSession) -> None:
    result = await session.execute(
        select(FreeformConfig).where(FreeformConfig.enabled == True)  # noqa: E712
    )
    configs = result.scalars().all()
    now = datetime.now(UTC)

    for config in configs:
        if _is_due(config, now):
            log.info(f"PO analysis due for repo_id={config.repo_id}")
            try:
                brief = await _ensure_brief(session, config)
                if brief is None:
                    repo = (
                        await session.execute(
                            select(Repo).where(Repo.id == config.repo_id)
                        )
                    ).scalar_one_or_none()
                    await publish(
                        po_analysis_failed(
                            repo_name=repo.name if repo else "?",
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
                # Back off on failure: advance last_analysis_at so we don't
                # re-clone + re-invoke the agent every CHECK_INTERVAL
                # seconds against a perma-broken repo.
                if _FAILURE_BACKOFF_NOW:
                    config.last_analysis_at = now
                    await session.commit()


def _is_due(config: FreeformConfig, now: datetime) -> bool:
    if config.last_analysis_at is None:
        return True
    base_time = config.last_analysis_at
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=UTC)
    cron = croniter(config.analysis_cron, base_time)
    next_run = cron.get_next(datetime)
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=UTC)
    return now >= next_run


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


async def _ensure_brief(
    session: AsyncSession, config: FreeformConfig
) -> MarketBrief | None:
    """Return a fresh MarketBrief for `config.repo_id`.

    Returns the latest existing brief if it's within `market_brief_max_age_days`.
    Otherwise runs the researcher. If the researcher fails, falls back to the
    most recent prior brief (even if stale). Returns None if nothing exists.
    """
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


async def handle_po_analysis(
    session: AsyncSession, config: FreeformConfig, *, brief: MarketBrief
) -> None:
    """Run the agent as a Product Owner to analyze a repo and generate suggestions.

    Uses readonly tools so the PO agent can explore the codebase (grep, file_read, glob)
    rather than relying on a black-box CLI subprocess.
    """
    repo_result = await session.execute(select(Repo).where(Repo.id == config.repo_id))
    repo = repo_result.scalar_one_or_none()
    if not repo:
        log.warning(f"Repo not found for freeform config id={config.id}")
        return

    recent_result = await session.execute(
        select(Suggestion.title)
        .where(Suggestion.repo_id == config.repo_id)
        .order_by(Suggestion.created_at.desc())
        .limit(50)
    )
    recent_titles = [row[0] for row in recent_result.all()]

    ws_name = f"po-{repo.name.replace('/', '-')}"
    workspace = await clone_repo(
        repo.url, 0, config.dev_branch or repo.default_branch, workspace_name=ws_name
    )

    prompt = build_po_analysis_prompt(
        brief=brief,
        ux_knowledge=config.ux_knowledge,
        recent_suggestions=recent_titles,
        goal=config.po_goal,
    )

    # Notify UI
    await publish(po_analysis_started(repo_name=repo.name))

    log.info(f"Running PO analysis for repo '{repo.name}'")
    try:
        # Use readonly tools so the PO can explore the codebase
        agent = create_agent(
            workspace,
            readonly=True,
            max_turns=25,
            task_description=(
                f"Product Owner analysis of {repo.name}: surface the most "
                "impactful improvements."
            ),
            repo_name=repo.name,
        )
        result = await agent.run(prompt)
        output = result.output
    except Exception:
        log.exception(f"PO analysis for '{repo.name}' failed during agent execution")
        await publish(po_analysis_failed(repo_name=repo.name))
        raise

    suggestions_data = parse_json_response(output)
    if not suggestions_data:
        log.warning(f"PO analysis for '{repo.name}' returned no parseable output")
        await publish(
            po_analysis_failed(repo_name=repo.name, reason="No parseable output")
        )
        return

    new_suggestions = suggestions_data.get("suggestions", [])
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
        # Promote high-priority items into the shared knowledge graph so the
        # next planning/coding agent for this repo sees them as known issues.
        await remember_priority_suggestion(
            repo_name=repo.name,
            title=suggestion.title,
            rationale=suggestion.rationale,
            priority=suggestion.priority,
            category=f"PO suggestion / {suggestion.category}",
            source="po-analyzer",
        )

    ux_update = suggestions_data.get("ux_knowledge_update")
    if ux_update:
        config.ux_knowledge = ux_update

    await session.flush()
    log.info(f"PO analysis for '{repo.name}': {len(filtered)} suggestions created ({dropped} ungrounded dropped)")

    await publish(
        po_suggestions_ready(repo_name=repo.name, count=len(filtered))
    )
