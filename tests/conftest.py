"""Pytest configuration shared across the unit suite.

Provides autouse fixtures that install fresh in-memory adapters for the
two cross-cutting Redis seams (the broadcast Publisher and the per-task
TaskChannel) so no test ever accidentally hits real Redis. Tests that
want to inspect captured state can grab the active adapter via the
named fixtures (``publisher``, ``task_channel``) or the seam's
``get_*`` helpers.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from shared import events as _events_mod
from shared import task_channel as _task_channel_mod
from shared.events import InMemoryPublisher, set_publisher
from shared.task_channel import InMemoryTaskChannelFactory, set_task_channel_factory


@pytest.fixture(autouse=True)
def _isolated_publisher():
    """Swap in a fresh InMemoryPublisher per test, restore after."""
    previous = _events_mod._publisher
    pub = InMemoryPublisher()
    set_publisher(pub)
    try:
        yield pub
    finally:
        _events_mod._publisher = previous


@pytest.fixture
def publisher(_isolated_publisher: InMemoryPublisher) -> InMemoryPublisher:
    """Convenience handle to the per-test InMemoryPublisher."""
    return _isolated_publisher


@pytest.fixture(autouse=True)
def _isolated_task_channel():
    """Swap in a fresh InMemoryTaskChannelFactory per test, restore after.

    Mirrors ``_isolated_publisher``. Tests that want to inspect the
    captured state grab the factory via the ``task_channel`` fixture and
    assert against ``factory.guidance``, ``factory.heartbeats``,
    ``factory.streams``, or ``factory.telegram_bindings``.
    """
    previous = _task_channel_mod._factory
    factory = InMemoryTaskChannelFactory()
    set_task_channel_factory(factory)
    try:
        yield factory
    finally:
        _task_channel_mod._factory = previous


@pytest.fixture
def task_channel(_isolated_task_channel: InMemoryTaskChannelFactory) -> InMemoryTaskChannelFactory:
    """Convenience handle to the per-test InMemoryTaskChannelFactory."""
    return _isolated_task_channel


@pytest_asyncio.fixture
async def session():
    """Real-DB session that rolls back at end of test.

    Requires DATABASE_URL pointing at a writable Postgres (CI + docker compose).
    Skips locally if unset so the mock-based suite still passes standalone.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set — Phase 4 DB tests need real Postgres")

    from shared.database import async_session

    async with async_session() as s, s.begin():
        yield s
        await s.rollback()
