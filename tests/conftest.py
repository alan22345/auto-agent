"""Pytest configuration shared across the unit suite.

Provides an autouse fixture that installs a fresh ``InMemoryPublisher`` for
every test, so no test ever accidentally hits the real Redis stream. Tests
that want to inspect published events can grab the active publisher via
``shared.events.get_publisher()`` (or use the ``publisher`` fixture).
"""

from __future__ import annotations

import pytest

from shared import events as _events_mod
from shared.events import InMemoryPublisher, set_publisher


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
