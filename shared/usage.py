"""Append a `usage_events` row for each accountable event.

For v1 we only emit `kind="llm_call"`. The function is async and tolerates
failure: a DB write error is logged and swallowed — quota accounting failure
must NOT crash an in-flight task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from shared.models import UsageEvent
from shared.pricing import estimate_cost_cents

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


async def emit_usage_event(
    *,
    org_id: int,
    task_id: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    kind: str = "llm_call",
    session: AsyncSession | None = None,
) -> None:
    """Insert one usage_events row. Best-effort; logs and continues on error.

    If *session* is provided the caller owns the transaction — the row is
    flushed but NOT committed here.  When *session* is None the function
    opens its own session and commits.
    """
    cost = estimate_cost_cents(model, input_tokens, output_tokens)
    event = UsageEvent(
        org_id=org_id,
        task_id=task_id,
        kind=kind,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_cents=cost,
    )

    if session is not None:
        # Caller supplies the transaction — just flush, don't commit.
        try:
            session.add(event)
            await session.flush()
        except Exception:
            log.warning(
                "usage_event_write_failed",
                org_id=org_id,
                task_id=task_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        return

    # No caller session — open our own and commit.
    try:
        from shared.database import async_session as _session_factory

        async with _session_factory() as s:
            s.add(event)
            await s.commit()
    except Exception:  # best-effort: swallow all DB errors
        log.warning(
            "usage_event_write_failed",
            org_id=org_id,
            task_id=task_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
