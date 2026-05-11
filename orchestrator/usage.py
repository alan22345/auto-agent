"""Append a `usage_events` row for each accountable event.

For v1 we only emit `kind="llm_call"`. The function is async and tolerates
failure: a DB write error is logged and swallowed — quota accounting failure
must NOT crash an in-flight task.
"""

from __future__ import annotations

import structlog

from shared.database import async_session
from shared.models import UsageEvent
from shared.pricing import estimate_cost_cents

log = structlog.get_logger(__name__)


async def emit_usage_event(
    *,
    org_id: int,
    task_id: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    kind: str = "llm_call",
) -> None:
    """Insert one usage_events row. Best-effort; logs and continues on error."""
    cost = estimate_cost_cents(model, input_tokens, output_tokens)
    try:
        async with async_session() as session:
            session.add(
                UsageEvent(
                    org_id=org_id,
                    task_id=task_id,
                    kind=kind,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_cents=cost,
                )
            )
            await session.commit()
    except Exception:  # best-effort: swallow all DB errors
        log.warning(
            "usage_event_write_failed",
            org_id=org_id,
            task_id=task_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
