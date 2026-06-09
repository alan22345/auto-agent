"""Phase 4 — VM-global exclusive lease (fake-redis unit tests)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.health_loop import lease


class FakeRedis:
    """Minimal async redis stand-in supporting set(nx,ex)/get/delete."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value if isinstance(value, bytes) else str(value).encode()
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        return 1


@pytest.fixture
def fake_redis():
    fr = FakeRedis()
    with patch.object(lease, "get_redis", AsyncMock(return_value=fr)):
        yield fr


@pytest.mark.asyncio
async def test_acquire_succeeds_when_free_then_blocks_others(fake_redis):
    assert await lease.acquire_lease("supervisor-1", ttl_seconds=60) is True
    # A different holder cannot acquire while held.
    assert await lease.acquire_lease("supervisor-2", ttl_seconds=60) is False


@pytest.mark.asyncio
async def test_lease_held_and_holder_reflect_state(fake_redis):
    assert await lease.lease_held() is False
    assert await lease.lease_holder() is None
    await lease.acquire_lease("supervisor-1", ttl_seconds=60)
    assert await lease.lease_held() is True
    assert await lease.lease_holder() == "supervisor-1"
