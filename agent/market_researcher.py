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

from agent.lifecycle.factory import create_agent
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

    fetched_urls = _raw_sources_from_messages(result)

    # Count turns as number of assistant messages in the conversation
    agent_turns = sum(1 for m in result.messages if m.role == "assistant")

    # Empty / minimal payload → still persist, mark partial so PO knows
    has_real_content = any(
        data.get(k) for k in (
            "competitors", "findings", "modality_gaps", "strategic_themes"
        )
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
        agent_turns=agent_turns,
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


def _raw_sources_from_messages(agent_result) -> list[dict]:
    """Extract fetch_url tool-call telemetry from the agent's message history.

    We scan assistant messages for tool_calls with name == "fetch_url" and
    collect each unique URL.  This is deterministic and can't be forgotten by
    the agent, unlike relying on the agent to mention URLs in its text output.

    Note: AgentResult.messages contains the full conversation; assistant
    messages carry tool_calls (list[ToolCall]) when the model invoked tools.
    """
    messages = getattr(agent_result, "messages", None) or []
    sources: list[dict] = []
    seen: set[str] = set()
    for msg in messages:
        if msg.role != "assistant":
            continue
        for tc in msg.tool_calls or []:
            if tc.name != "fetch_url":
                continue
            url = tc.arguments.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append({
                "url": url,
                "title": "",
                "fetched_at": datetime.now(UTC).isoformat(),
            })
    return sources
