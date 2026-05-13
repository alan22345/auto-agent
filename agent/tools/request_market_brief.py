"""Architect-side tool to request a market brief, wrapping market_researcher.

The real `agent.market_researcher.run_market_research` takes
``(session, config, repo)`` — a DB session, a FreeformConfig ORM row, and a
Repo ORM row.  This wrapper resolves those objects from the task_id carried
in the ToolContext, then delegates.

The module-level ``run_market_research`` async function is defined here so
that tests can patch ``agent.tools.request_market_brief.run_market_research``
without touching the real market_researcher module.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar

from agent.tools.base import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)


async def run_market_research(*, task_id: int, product_description: str) -> dict:
    """Resolve DB objects from task_id and delegate to the real researcher.

    Raises ``ValueError`` if the task, its repo, or the repo's FreeformConfig
    cannot be found.  Returns a plain dict with at least a ``"summary"`` key
    (mirrors the MarketBrief fields).
    """
    from sqlalchemy import select

    from agent.market_researcher import run_market_research as _real
    from shared.database import async_session
    from shared.models import FreeformConfig, Repo
    from shared.models.core import Task

    async with async_session() as session:
        # Load task → repo
        task_row = await session.get(Task, task_id)
        if task_row is None:
            raise ValueError(f"Task {task_id} not found")

        repo_id = task_row.repo_id
        if repo_id is None:
            raise ValueError(f"Task {task_id} has no associated repo")

        repo = await session.get(Repo, repo_id)
        if repo is None:
            raise ValueError(f"Repo {repo_id} not found for task {task_id}")

        # Load FreeformConfig for this repo (may not exist for non-freeform repos)
        config_result = await session.execute(
            select(FreeformConfig).where(FreeformConfig.repo_id == repo_id)
        )
        config = config_result.scalar_one_or_none()
        if config is None:
            raise ValueError(
                f"No FreeformConfig found for repo {repo_id}. "
                "Market research requires freeform mode to be configured."
            )

        brief = await _real(session, config, repo)

    if brief is None:
        return {"summary": "Market research failed — see logs for details."}

    return {
        "brief_id": brief.id,
        "summary": brief.summary or "",
        "product_category": brief.product_category,
        "competitors": brief.competitors,
        "findings": brief.findings,
        "modality_gaps": brief.modality_gaps,
        "strategic_themes": brief.strategic_themes,
        "partial": brief.partial,
    }


class RequestMarketBriefTool(Tool):
    name = "request_market_brief"
    description = (
        "Request a market-research brief about the product/UX shape implied by "
        "the task. Call this during initial architecture design (or revision) "
        "when the task involves product or UX decisions and the right shape "
        "isn't obvious from the task description. The brief is stored as a "
        "MarketBrief row attached to the parent task's repo; cite it in "
        "ARCHITECTURE.md."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "product_description": {
                "type": "string",
                "description": (
                    "What this product is, in your own words. "
                    "The researcher uses this as its query."
                ),
            },
        },
        "required": ["product_description"],
    }
    is_readonly = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.task_id is None:
            return ToolResult(
                output="request_market_brief requires a task context.",
                is_error=True,
            )

        try:
            payload = await run_market_research(
                task_id=context.task_id,
                product_description=arguments["product_description"],
            )
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except Exception:
            log.exception(
                "request_market_brief: unexpected error task_id=%s", context.task_id
            )
            return ToolResult(
                output="Market research failed unexpectedly — see logs.",
                is_error=True,
            )

        summary = payload.get("summary", str(payload))
        return ToolResult(output=summary, token_estimate=len(summary) // 4)
