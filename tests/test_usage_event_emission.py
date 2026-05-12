"""agent.loop.AgentLoop emits one usage_events row per provider.complete() call."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from agent.llm.types import LLMResponse, Message, TokenUsage
from agent.loop import AgentLoop, UsageSink
from shared.models import UsageEvent

pytestmark = pytest.mark.asyncio


class _FakeProvider:
    is_passthrough = True   # use the passthrough path — minimal scaffolding
    model = "claude-sonnet-4-6"
    max_context_tokens = 200_000

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            message=Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=100, output_tokens=50),
        )


async def test_usage_row_written_per_call(session) -> None:
    from tests.helpers import make_org_and_task

    slug = f"usage-emit-{uuid.uuid4().hex[:8]}"
    org, task = await make_org_and_task(session, slug=slug)
    # flush to assign IDs — stay inside the test transaction so rollback cleans up
    await session.flush()

    # Pass db_session so emit_usage_event uses our transaction (flush, not commit).
    # This keeps the row inside the test's transaction, so the fixture rollback
    # cleans up automatically — no manual delete needed.
    sink = UsageSink(org_id=org.id, task_id=task.id, db_session=session)
    loop = AgentLoop(
        provider=_FakeProvider(),
        tools=None,
        context_manager=None,
        workspace="/tmp",
        usage_sink=sink,
    )
    await loop.run("hi")

    rows = (await session.execute(
        select(UsageEvent).where(UsageEvent.org_id == org.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].input_tokens == 100
    assert rows[0].output_tokens == 50
    assert rows[0].model == "claude-sonnet-4-6"
    assert rows[0].task_id == task.id
    # No manual cleanup needed — all rows are inside the test's rolled-back transaction.
