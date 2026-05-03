"""Product Owner analyzer — periodically analyzes repos to generate improvement suggestions.

Uses the AgentLoop with readonly tools so the PO agent can explore the codebase
with grep, file_read, etc.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from croniter import croniter
from sqlalchemy import select

from agent.prompts import build_po_analysis_prompt
from agent.workspace import clone_repo
from shared.database import async_session
from shared.events import Event, publish
from shared.models import FreeformConfig, Repo, Suggestion, SuggestionStatus

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
                await handle_po_analysis(session, config)
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


async def handle_po_analysis(session: AsyncSession, config: FreeformConfig) -> None:
    """Run the agent as a Product Owner to analyze a repo and generate suggestions.

    Uses readonly tools so the PO agent can explore the codebase (grep, file_read, glob)
    rather than relying on a black-box CLI subprocess.
    """
    from agent.main import _create_agent

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
    workspace = await clone_repo(repo.url, 0, config.dev_branch or repo.default_branch, workspace_name=ws_name)

    prompt = build_po_analysis_prompt(
        ux_knowledge=config.ux_knowledge,
        recent_suggestions=recent_titles,
    )

    # Notify UI
    await publish(
        Event(type="po.analysis_started", task_id=0, payload={"repo_name": repo.name})
    )

    log.info(f"Running PO analysis for repo '{repo.name}'")
    try:
        # Use readonly tools so the PO can explore the codebase
        agent = _create_agent(workspace, readonly=True, max_turns=25)
        result = await agent.run(prompt)
        output = result.output
    except Exception:
        log.exception(f"PO analysis for '{repo.name}' failed during agent execution")
        await publish(
            Event(type="po.analysis_failed", task_id=0, payload={"repo_name": repo.name})
        )
        raise

    suggestions_data = _parse_analysis_output(output)
    if not suggestions_data:
        log.warning(f"PO analysis for '{repo.name}' returned no parseable output")
        await publish(
            Event(
                type="po.analysis_failed",
                task_id=0,
                payload={"repo_name": repo.name, "reason": "No parseable output"},
            )
        )
        return

    new_suggestions = suggestions_data.get("suggestions", [])
    for s in new_suggestions:
        suggestion = Suggestion(
            repo_id=config.repo_id,
            title=s.get("title", "Untitled"),
            description=s.get("description", ""),
            rationale=s.get("rationale", ""),
            category=s.get("category", "improvement"),
            priority=s.get("priority", 3),
            status=SuggestionStatus.PENDING,
        )
        session.add(suggestion)

    ux_update = suggestions_data.get("ux_knowledge_update")
    if ux_update:
        config.ux_knowledge = ux_update

    await session.flush()
    log.info(f"PO analysis for '{repo.name}': {len(new_suggestions)} suggestions created")

    await publish(
        Event(
            type="po.suggestions_ready",
            task_id=0,
            payload={"repo_name": repo.name, "count": len(new_suggestions)},
        )
    )


def _parse_analysis_output(output: str) -> dict | None:
    """Parse JSON output from PO analysis, handling markdown fences."""
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
        log.warning(f"Failed to parse PO analysis JSON: {text[:200]}...")
        return None
