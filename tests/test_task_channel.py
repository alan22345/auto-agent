"""Tests for the TaskChannel seam (shared/task_channel.py).

Two adapters live behind the seam — one for production (Redis) and one
for tests (in-memory). Both adapters round-trip the same verbs.

The Redis-adapter tests use ``AsyncMock`` for the Redis client so we
don't depend on a running server; they pin the wire format (key strings,
JSON shape, TTLs) so the consumer side in ``web/main.py`` keeps working.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from shared.task_channel import (
    HEARTBEAT_TTL_SECONDS,
    TASK_STREAM_PATTERN,
    TELEGRAM_BINDING_TTL_SECONDS,
    InMemoryTaskChannel,
    InMemoryTaskChannelFactory,
    RedisTaskChannel,
    RedisTaskChannelFactory,
    get_task_channel_factory,
    set_task_channel_factory,
    task_channel,
    task_id_for_telegram_message,
)

# ---------------------------------------------------------------------------
# In-memory adapter (the second adapter that pays for itself in tests)
# ---------------------------------------------------------------------------


class TestInMemoryAdapter:
    @pytest.mark.asyncio
    async def test_push_then_pop_round_trip(self):
        f = InMemoryTaskChannelFactory()
        ch = f.for_task(7)
        await ch.push_guidance("first")
        await ch.push_guidance("second")
        assert await ch.pop_guidance() == "first"
        assert await ch.pop_guidance() == "second"
        assert await ch.pop_guidance() is None

    @pytest.mark.asyncio
    async def test_pop_returns_none_when_empty(self):
        f = InMemoryTaskChannelFactory()
        assert await f.for_task(99).pop_guidance() is None

    @pytest.mark.asyncio
    async def test_guidance_is_per_task(self):
        f = InMemoryTaskChannelFactory()
        await f.for_task(1).push_guidance("for-one")
        await f.for_task(2).push_guidance("for-two")
        assert await f.for_task(1).pop_guidance() == "for-one"
        assert await f.for_task(2).pop_guidance() == "for-two"

    @pytest.mark.asyncio
    async def test_heartbeat_then_is_alive(self):
        f = InMemoryTaskChannelFactory()
        ch = f.for_task(3)
        assert await ch.is_alive() is False
        await ch.heartbeat()
        assert await ch.is_alive() is True

    @pytest.mark.asyncio
    async def test_is_alive_honours_ttl(self):
        f = InMemoryTaskChannelFactory()
        # Stamp a heartbeat older than the TTL by hand
        f.heartbeats[5] = datetime.now(UTC) - timedelta(
            seconds=HEARTBEAT_TTL_SECONDS + 60
        )
        assert await f.for_task(5).is_alive() is False

    @pytest.mark.asyncio
    async def test_stream_tool_call_captures_payload(self):
        f = InMemoryTaskChannelFactory()
        await f.for_task(1).stream_tool_call(
            tool="grep",
            args_preview='"foo" in src/',
            result_preview="match in main.py",
            turn=4,
        )
        assert f.streams == [
            (
                1,
                "tool",
                {
                    "tool": "grep",
                    "args_preview": '"foo" in src/',
                    "result_preview": "match in main.py",
                    "turn": 4,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_stream_thinking_captures_payload(self):
        f = InMemoryTaskChannelFactory()
        await f.for_task(2).stream_thinking(text="planning the refactor", turn=1)
        assert f.streams == [
            (2, "thinking", {"text": "planning the refactor", "turn": 1})
        ]

    @pytest.mark.asyncio
    async def test_telegram_bind_then_lookup(self):
        f = InMemoryTaskChannelFactory()
        await f.for_task(42).bind_telegram_message(message_id=999)
        assert await f.task_id_for_telegram_message(999) == 42
        assert await f.task_id_for_telegram_message(1000) is None

    @pytest.mark.asyncio
    async def test_aclose_clears_state(self):
        f = InMemoryTaskChannelFactory()
        await f.for_task(1).push_guidance("x")
        await f.for_task(1).heartbeat()
        await f.for_task(1).bind_telegram_message(7)
        await f.aclose()
        assert f.guidance == {}
        assert f.heartbeats == {}
        assert f.streams == []
        assert f.telegram_bindings == {}


# ---------------------------------------------------------------------------
# Redis adapter — pin the wire format so web/main.py stays working
# ---------------------------------------------------------------------------


def _factory_with_mock(client: AsyncMock) -> RedisTaskChannelFactory:
    """Build a RedisTaskChannelFactory whose lazy-loaded client is the
    given mock — bypasses the real ``redis.asyncio.from_url`` import."""
    f = RedisTaskChannelFactory(url="redis://unused")
    f._client = client
    return f


class TestRedisAdapterWireFormat:
    @pytest.mark.asyncio
    async def test_push_guidance_uses_correct_key(self):
        client = AsyncMock()
        f = _factory_with_mock(client)
        await f.for_task(11).push_guidance("hello")
        client.rpush.assert_awaited_once_with("task:11:guidance", "hello")

    @pytest.mark.asyncio
    async def test_pop_guidance_decodes_bytes(self):
        client = AsyncMock()
        client.lpop.return_value = b"queued message"
        f = _factory_with_mock(client)
        result = await f.for_task(11).pop_guidance()
        assert result == "queued message"
        client.lpop.assert_awaited_once_with("task:11:guidance")

    @pytest.mark.asyncio
    async def test_pop_guidance_returns_none_when_empty(self):
        client = AsyncMock()
        client.lpop.return_value = None
        f = _factory_with_mock(client)
        assert await f.for_task(11).pop_guidance() is None

    @pytest.mark.asyncio
    async def test_pop_guidance_swallows_errors(self):
        client = AsyncMock()
        client.lpop.side_effect = ConnectionError("redis down")
        f = _factory_with_mock(client)
        assert await f.for_task(11).pop_guidance() is None

    @pytest.mark.asyncio
    async def test_heartbeat_sets_key_with_15_minute_ttl(self):
        client = AsyncMock()
        f = _factory_with_mock(client)
        await f.for_task(11).heartbeat()
        client.set.assert_awaited_once_with("task:11:heartbeat", "1", ex=900)

    @pytest.mark.asyncio
    async def test_is_alive_checks_existence(self):
        client = AsyncMock()
        client.exists.return_value = 1
        f = _factory_with_mock(client)
        assert await f.for_task(11).is_alive() is True
        client.exists.assert_awaited_once_with("task:11:heartbeat")

    @pytest.mark.asyncio
    async def test_is_alive_returns_false_on_error(self):
        client = AsyncMock()
        client.exists.side_effect = ConnectionError("redis down")
        f = _factory_with_mock(client)
        assert await f.for_task(11).is_alive() is False

    @pytest.mark.asyncio
    async def test_stream_tool_call_publishes_json(self):
        client = AsyncMock()
        f = _factory_with_mock(client)
        await f.for_task(11).stream_tool_call(
            tool="bash",
            args_preview="ls",
            result_preview="file1\nfile2",
            turn=2,
        )
        client.publish.assert_awaited_once()
        channel, payload = client.publish.await_args.args
        assert channel == "task:11:stream"
        decoded = json.loads(payload)
        assert decoded == {
            "type": "tool",
            "tool": "bash",
            "args_preview": "ls",
            "result_preview": "file1\nfile2",
            "turn": 2,
        }

    @pytest.mark.asyncio
    async def test_stream_thinking_publishes_json(self):
        client = AsyncMock()
        f = _factory_with_mock(client)
        await f.for_task(11).stream_thinking(text="a thought", turn=5)
        client.publish.assert_awaited_once()
        channel, payload = client.publish.await_args.args
        assert channel == "task:11:stream"
        assert json.loads(payload) == {
            "type": "thinking",
            "text": "a thought",
            "turn": 5,
        }

    @pytest.mark.asyncio
    async def test_bind_telegram_message_uses_7_day_ttl(self):
        client = AsyncMock()
        f = _factory_with_mock(client)
        await f.for_task(42).bind_telegram_message(999)
        client.set.assert_awaited_once_with(
            "telegram:msg:999", "42", ex=TELEGRAM_BINDING_TTL_SECONDS
        )
        assert TELEGRAM_BINDING_TTL_SECONDS == 7 * 24 * 60 * 60

    @pytest.mark.asyncio
    async def test_task_id_for_telegram_message_decodes_int(self):
        client = AsyncMock()
        client.get.return_value = b"42"
        f = _factory_with_mock(client)
        assert await f.task_id_for_telegram_message(999) == 42
        client.get.assert_awaited_once_with("telegram:msg:999")

    @pytest.mark.asyncio
    async def test_task_id_for_telegram_message_returns_none_when_missing(self):
        client = AsyncMock()
        client.get.return_value = None
        f = _factory_with_mock(client)
        assert await f.task_id_for_telegram_message(999) is None

    @pytest.mark.asyncio
    async def test_task_id_for_telegram_message_swallows_errors(self):
        client = AsyncMock()
        client.get.side_effect = ConnectionError("redis down")
        f = _factory_with_mock(client)
        assert await f.task_id_for_telegram_message(999) is None


class TestFactoryFacade:
    """Module-level set/get/task_channel/task_id_for_telegram_message."""

    @pytest.mark.asyncio
    async def test_task_channel_routes_through_active_factory(self):
        f = InMemoryTaskChannelFactory()
        set_task_channel_factory(f)
        await task_channel(13).push_guidance("x")
        assert f.guidance[13] == ["x"]

    @pytest.mark.asyncio
    async def test_task_id_for_telegram_message_routes_through_active_factory(self):
        f = InMemoryTaskChannelFactory()
        f.telegram_bindings[123] = 99
        set_task_channel_factory(f)
        assert await task_id_for_telegram_message(123) == 99

    def test_get_factory_raises_when_unset(self):
        import shared.task_channel as mod

        previous = mod._factory
        mod._factory = None
        try:
            with pytest.raises(RuntimeError, match="No TaskChannelFactory registered"):
                get_task_channel_factory()
        finally:
            mod._factory = previous


def test_stream_pattern_matches_key_scheme():
    """The wildcard the consumer uses must match the per-task channel
    name. If the key scheme ever drifts, this catches it."""
    from shared.task_channel import _stream_channel

    channel = _stream_channel(123)
    # TASK_STREAM_PATTERN is "task:*:stream"
    prefix, suffix = TASK_STREAM_PATTERN.split("*")
    assert channel.startswith(prefix) and channel.endswith(suffix)


class TestInMemoryAdapterImplementsProtocol:
    """Sanity check that the in-memory adapter exposes the same
    ``task_id`` attribute and verbs the production one does."""

    def test_per_task_handle_carries_task_id(self):
        f = InMemoryTaskChannelFactory()
        ch = f.for_task(77)
        assert isinstance(ch, InMemoryTaskChannel)
        assert ch.task_id == 77

    def test_redis_per_task_handle_carries_task_id(self):
        f = RedisTaskChannelFactory("redis://unused")
        ch = f.for_task(77)
        assert isinstance(ch, RedisTaskChannel)
        assert ch.task_id == 77
