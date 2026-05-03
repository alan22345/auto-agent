"""Architecture-mode analyzer — periodically runs improve-codebase-architecture.

Parallel to ``agent/po_analyzer.py``: same shape, different lens. When
``FreeformConfig.architecture_mode`` is True for a repo, the cron schedule on
``architecture_cron`` triggers the agent to walk the codebase, apply the
deepening lens, and produce up to 5 ``Suggestion`` rows with
``category='architecture'``.

If the repo also has ``auto_approve_suggestions = True``, the existing
suggestion → task auto-approval path turns these into Tasks. Architecture
tasks arrive with ``intake_qa = []`` so the planning agent skips the grill
phase (the analyzer has already grilled itself).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from croniter import croniter
from sqlalchemy import select

from agent.prompts import build_architecture_analysis_prompt
from agent.workspace import clone_repo
from shared.database import async_session
from shared.events import Event, publish
from shared.models import FreeformConfig, Repo, Suggestion, SuggestionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds

# After a failed analysis, advance last_architecture_at so the next try
# happens on the regular cron schedule rather than retrying every CHECK_INTERVAL.
# Without this back-off a perma-broken repo would clone + invoke the agent
# every 60s. Mirrors the same back-off in po_analyzer.
_FAILURE_BACKOFF_NOW = True


async def run_architecture_loop() -> None:
    """Background loop — check freeform configs and run architecture analysis when due."""
    log.info("Architecture analysis loop started")
    while True:
        try:
            async with async_session() as session:
                await _check_and_analyze(session)
        except Exception:
            log.exception("Architecture analysis loop error")
        await asyncio.sleep(CHECK_INTERVAL)


async def _check_and_analyze(session: AsyncSession) -> None:
    result = await session.execute(
        select(FreeformConfig).where(FreeformConfig.architecture_mode == True)  # noqa: E712
    )
    configs = result.scalars().all()
    now = datetime.now(UTC)

    for config in configs:
        if _is_due(config, now):
            log.info(f"Architecture analysis due for repo_id={config.repo_id}")
            try:
                await handle_architecture_analysis(session, config)
                config.last_architecture_at = now
                await session.commit()
            except Exception:
                log.exception(f"Architecture analysis failed for repo_id={config.repo_id}")
                # Back off on failure: advance last_architecture_at so we
                # don't re-clone + re-invoke the agent every CHECK_INTERVAL
                # seconds for a perma-broken repo. The next attempt waits
                # for the regular cron tick.
                if _FAILURE_BACKOFF_NOW:
                    config.last_architecture_at = now
                    await session.commit()


def _is_due(config: FreeformConfig, now: datetime) -> bool:
    """True iff the configured cron has fired since last_architecture_at."""
    if config.last_architecture_at is None:
        return True
    base_time = config.last_architecture_at
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=UTC)
    cron = croniter(config.architecture_cron, base_time)
    next_run = cron.get_next(datetime)
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=UTC)
    return now >= next_run


async def handle_architecture_analysis(session: AsyncSession, config: FreeformConfig) -> None:
    """Run the agent as an architectural reviewer, producing deepening Suggestions.

    Uses readonly tools so the agent can explore the codebase but not modify it.
    Deepening proposals become Suggestions; if auto-approval is on, they become
    Tasks via the existing suggestion → task path.
    """
    from agent.lifecycle._agent import create_agent

    repo_result = await session.execute(select(Repo).where(Repo.id == config.repo_id))
    repo = repo_result.scalar_one_or_none()
    if not repo:
        log.warning(f"Repo not found for freeform config id={config.id}")
        return

    recent_result = await session.execute(
        select(Suggestion.title)
        .where(Suggestion.repo_id == config.repo_id)
        .where(Suggestion.category == "architecture")
        .order_by(Suggestion.created_at.desc())
        .limit(50)
    )
    recent_titles = [row[0] for row in recent_result.all()]

    ws_name = f"arch-{repo.name.replace('/', '-')}"
    workspace = await clone_repo(
        repo.url, 0, config.dev_branch or repo.default_branch, workspace_name=ws_name
    )

    prompt = build_architecture_analysis_prompt(
        architecture_knowledge=config.architecture_knowledge,
        recent_suggestions=recent_titles,
    )

    await publish(
        Event(
            type="architecture.analysis_started",
            task_id=0,
            payload={"repo_name": repo.name},
        )
    )

    log.info(f"Running architecture analysis for repo '{repo.name}'")
    try:
        agent = create_agent(workspace, readonly=True, max_turns=30, include_methodology=True)
        result = await agent.run(prompt)
        output = result.output
    except Exception:
        log.exception(f"Architecture analysis for '{repo.name}' failed during agent execution")
        await publish(
            Event(
                type="architecture.analysis_failed",
                task_id=0,
                payload={"repo_name": repo.name},
            )
        )
        raise

    suggestions_data = _parse_analysis_output(output)
    if not suggestions_data:
        log.warning(f"Architecture analysis for '{repo.name}' returned no parseable output")
        await publish(
            Event(
                type="architecture.analysis_failed",
                task_id=0,
                payload={"repo_name": repo.name, "reason": "No parseable output"},
            )
        )
        return

    new_suggestions = suggestions_data.get("suggestions", [])
    for s in new_suggestions:
        suggestion = Suggestion(
            repo_id=config.repo_id,
            title=s.get("title", "Untitled deepening"),
            description=s.get("description", ""),
            rationale=s.get("rationale", ""),
            # Force category — the prompt instructs the agent to use this, but
            # we lock it down here so downstream auto-approval can route on it.
            category="architecture",
            priority=s.get("priority", 3),
            status=SuggestionStatus.PENDING,
        )
        session.add(suggestion)

    knowledge_update = suggestions_data.get("architecture_knowledge_update")
    if knowledge_update:
        config.architecture_knowledge = knowledge_update

    await session.flush()
    log.info(
        f"Architecture analysis for '{repo.name}': {len(new_suggestions)} suggestions created"
    )

    await publish(
        Event(
            type="architecture.suggestions_ready",
            task_id=0,
            payload={"repo_name": repo.name, "count": len(new_suggestions)},
        )
    )


def _parse_analysis_output(output: str) -> dict | None:
    """Parse JSON output from architecture analysis, handling markdown fences."""
    text = output.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None

    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log.warning(f"Failed to parse architecture analysis JSON: {text[:200]}...")
        return None
