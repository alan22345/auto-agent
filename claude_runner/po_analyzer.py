"""Product Owner analyzer — periodically analyzes repos to generate improvement suggestions."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.database import async_session
from shared.events import Event
from shared.models import FreeformConfig, Repo, Suggestion, SuggestionStatus
from shared.redis_client import get_redis, publish_event

from claude_runner.main import run_claude_code
from claude_runner.prompts import build_po_analysis_prompt
from claude_runner.workspace import WORKSPACES_DIR, clone_repo

log = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds


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
    """Check all enabled freeform configs and run analysis for any that are due."""
    result = await session.execute(
        select(FreeformConfig).where(FreeformConfig.enabled == True)
    )
    configs = result.scalars().all()
    now = datetime.now(timezone.utc)

    for config in configs:
        if _is_due(config, now):
            log.info(f"PO analysis due for repo_id={config.repo_id}")
            try:
                await handle_po_analysis(session, config)
                config.last_analysis_at = now
                await session.commit()
            except Exception:
                log.exception(f"PO analysis failed for repo_id={config.repo_id}")


def _is_due(config: FreeformConfig, now: datetime) -> bool:
    """Check if analysis should run now based on cron schedule."""
    base_time = config.last_analysis_at or config.created_at or now
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=timezone.utc)
    cron = croniter(config.analysis_cron, base_time)
    next_run = cron.get_next(datetime)
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    return now >= next_run


async def handle_po_analysis(session: AsyncSession, config: FreeformConfig) -> None:
    """Run Claude Code as a Product Owner to analyze a repo and generate suggestions."""
    # Load the repo
    repo_result = await session.execute(select(Repo).where(Repo.id == config.repo_id))
    repo = repo_result.scalar_one_or_none()
    if not repo:
        log.warning(f"Repo not found for freeform config id={config.id}")
        return

    # Load recent suggestion titles to avoid duplicates
    recent_result = await session.execute(
        select(Suggestion.title)
        .where(Suggestion.repo_id == config.repo_id)
        .order_by(Suggestion.created_at.desc())
        .limit(50)
    )
    recent_titles = [row[0] for row in recent_result.all()]

    # Clone/fetch repo for analysis — use repo-specific workspace to avoid collisions
    ws_name = f"po-{repo.name.replace('/', '-')}"
    workspace = await clone_repo(repo.url, 0, config.dev_branch or repo.default_branch, workspace_name=ws_name)

    # Build and run the PO analysis prompt
    prompt = build_po_analysis_prompt(
        ux_knowledge=config.ux_knowledge,
        recent_suggestions=recent_titles,
    )

    log.info(f"Running PO analysis for repo '{repo.name}'")
    output = await run_claude_code(workspace, prompt, timeout=900)

    # Parse JSON output
    suggestions_data = _parse_analysis_output(output)
    if not suggestions_data:
        log.warning(f"PO analysis for '{repo.name}' returned no parseable output")
        return

    # Insert suggestions
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

    # Update UX knowledge
    ux_update = suggestions_data.get("ux_knowledge_update")
    if ux_update:
        config.ux_knowledge = ux_update

    await session.flush()
    log.info(f"PO analysis for '{repo.name}': {len(new_suggestions)} suggestions created")

    # Notify UI
    r = await get_redis()
    await publish_event(
        r,
        Event(
            type="po.suggestions_ready",
            task_id=0,
            payload={"repo_name": repo.name, "count": len(new_suggestions)},
        ).to_redis(),
    )
    await r.aclose()


def _parse_analysis_output(output: str) -> dict | None:
    """Parse Claude's JSON output from PO analysis, handling markdown fences."""
    text = output.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first line (```json or ```) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try to find JSON object in the output
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None

    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log.warning(f"Failed to parse PO analysis JSON: {text[:200]}...")
        return None
