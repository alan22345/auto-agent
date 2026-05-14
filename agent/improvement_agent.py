"""Improvement agent — periodically runs improve-codebase-architecture.

Parallel to ``agent/po_analyzer.py``: same shape, different lens.
Formerly named ``architect_analyzer`` (the "architecture mode" feature).
Renamed by ADR-015 §14 — the trio's task-decomposer architect keeps the
name "architect" because that's a role within a flow, not a mode; this
module is a *mode* (the standing improvement / codebase-deepening one).

When ``FreeformConfig.architecture_mode`` is True for a repo, the cron
schedule on ``architecture_cron`` triggers the agent to walk the
codebase, apply the deepening lens, and produce up to 5 ``Suggestion``
rows with ``category='architecture'``. The DB category value stays
``"architecture"`` for backwards compatibility with existing rows; the
Python-side mode/role name is ``improvement_mode``.

If the repo also has ``auto_approve_suggestions = True``, the existing
suggestion → task auto-approval path turns these into Tasks.
Improvement tasks arrive with ``intake_qa = []`` so the planning agent
skips the grill phase (the analyzer has already grilled itself).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from croniter import croniter
from sqlalchemy import select

from agent.context.memory import remember_priority_suggestion
from agent.llm.structured import parse_json_response
from agent.prompts import build_architecture_analysis_prompt
from agent.workspace import clone_repo
from shared.database import async_session
from shared.events import (
    architecture_analysis_failed,
    architecture_analysis_started,
    architecture_suggestions_ready,
    publish,
)
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
    from agent.lifecycle.factory import create_agent

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

    await publish(architecture_analysis_started(repo_name=repo.name))

    log.info(f"Running architecture analysis for repo '{repo.name}'")
    try:
        agent = create_agent(
            workspace,
            readonly=True,
            max_turns=30,
            include_methodology=True,
            task_description=(
                f"Architectural deepening review of {repo.name}: identify "
                "modules that should be deepened, merged, or split."
            ),
            repo_name=repo.name,
        )
        result = await agent.run(prompt)
        output = result.output
    except Exception:
        log.exception(f"Architecture analysis for '{repo.name}' failed during agent execution")
        await publish(architecture_analysis_failed(repo_name=repo.name))
        raise

    suggestions_data = parse_json_response(output)
    if not suggestions_data:
        log.warning(f"Architecture analysis for '{repo.name}' returned no parseable output")
        await publish(
            architecture_analysis_failed(repo_name=repo.name, reason="No parseable output")
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
        # Promote high-priority deepenings into the shared knowledge graph so
        # the next agent working in this repo sees the known seams + smells.
        await remember_priority_suggestion(
            repo_name=repo.name,
            title=suggestion.title,
            rationale=suggestion.rationale,
            priority=suggestion.priority,
            category="architecture deepening",
            source="improvement-agent",
        )

    knowledge_update = suggestions_data.get("architecture_knowledge_update")
    if knowledge_update:
        config.architecture_knowledge = knowledge_update

    await session.flush()
    log.info(f"Architecture analysis for '{repo.name}': {len(new_suggestions)} suggestions created")

    await publish(architecture_suggestions_ready(repo_name=repo.name, count=len(new_suggestions)))


# ---------------------------------------------------------------------------
# Phase 10 freeform-mode gate entry points — ADR-015 §6.
#
# Sibling to ``agent/po_agent.py``'s ``po_*`` helpers. Thin wrappers that
# delegate to ``ImprovementAgentStandin``; production call sites
# go through ``agent.lifecycle.standin.run_freeform_gate`` for routing,
# these named helpers exist for code paths that already know they want
# the improvement-agent decision specifically.
#
# Each helper resumes the improvement agent's persisted session via the
# optional ``session_blob`` kwarg. When no blob is supplied the standin
# logs the ``fallback_default(source=heuristic)`` marker and proceeds —
# it never escapes to the user.
# ---------------------------------------------------------------------------


async def improvement_answer_grill(
    task,
    question: str,
    workspace_root: str,
    *,
    session_blob: dict | None = None,
) -> None:
    """Improvement-agent standin answers a grill question (freeform)."""

    from agent.lifecycle.standin import ImprovementAgentStandin

    repo = await _load_repo(task.repo_id)
    standin = ImprovementAgentStandin(task=task, repo=repo)
    await standin.answer_grill(
        question,
        {
            "workspace_root": workspace_root,
            "improvement_session": session_blob or {},
        },
    )


async def improvement_approve_plan(
    task,
    plan_md: str,
    workspace_root: str,
    *,
    session_blob: dict | None = None,
) -> None:
    """Improvement-agent standin approves/rejects a plan."""

    from agent.lifecycle.standin import ImprovementAgentStandin

    repo = await _load_repo(task.repo_id)
    standin = ImprovementAgentStandin(task=task, repo=repo)
    await standin.approve_plan(
        plan_md,
        {
            "workspace_root": workspace_root,
            "improvement_session": session_blob or {},
        },
    )


async def improvement_approve_design(
    task,
    design_md: str,
    workspace_root: str,
    *,
    session_blob: dict | None = None,
) -> None:
    """Improvement-agent standin approves/rejects a complex_large design."""

    from agent.lifecycle.standin import ImprovementAgentStandin

    repo = await _load_repo(task.repo_id)
    standin = ImprovementAgentStandin(task=task, repo=repo)
    await standin.approve_design(
        design_md,
        {
            "workspace_root": workspace_root,
            "improvement_session": session_blob or {},
        },
    )


async def improvement_review_pr(
    task,
    pr_diff: str,
    pr_metadata: dict,
    workspace_root: str,
    *,
    session_blob: dict | None = None,
) -> None:
    """Improvement-agent standin reviews a PR."""

    from agent.lifecycle.standin import ImprovementAgentStandin

    repo = await _load_repo(task.repo_id)
    standin = ImprovementAgentStandin(task=task, repo=repo)
    await standin.review_pr(
        pr_diff,
        pr_metadata,
        {
            "workspace_root": workspace_root,
            "improvement_session": session_blob or {},
        },
    )


async def _load_repo(repo_id: int | None):
    """Mirror of ``agent.po_agent._load_repo`` — fetch the Repo row,
    falling back to a stub when missing so the standin's heuristics
    still fire instead of raising."""

    if repo_id is None:
        return _MinimalRepo(id=None)
    async with async_session() as s:
        row = (await s.execute(select(Repo).where(Repo.id == repo_id))).scalar_one_or_none()
    return row or _MinimalRepo(id=repo_id)


class _MinimalRepo:
    """Same shape as ``po_agent._MinimalRepo``."""

    def __init__(
        self,
        *,
        id: int | None,  # noqa: A002 — mirrors ORM column name
    ) -> None:
        self.id = id
        self.product_brief = None
        self.mode = None
