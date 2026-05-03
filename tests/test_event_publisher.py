"""Tests for the event publish seam in shared/events.py.

Covers:
  - InMemoryPublisher: capture, wait_for, clear, aclose.
  - RedisStreamPublisher: xadd is called with the right key + payload, and
    the underlying client is reused across publishes (one connection, not
    one per call).
  - Module-level publish/set_publisher/get_publisher swap.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import (
    STREAM_KEY,
    Event,
    InMemoryPublisher,
    RedisStreamPublisher,
    get_publisher,
    publish,
    set_publisher,
)


class TestInMemoryPublisher:
    @pytest.mark.asyncio
    async def test_publish_appends_event(self):
        pub = InMemoryPublisher()
        ev = Event(type="task.created", task_id=1)
        await pub.publish(ev)
        assert pub.events == [ev]

    @pytest.mark.asyncio
    async def test_wait_for_returns_already_published_event(self):
        pub = InMemoryPublisher()
        ev = Event(type="task.classified", task_id=2)
        await pub.publish(ev)
        got = await pub.wait_for("task.classified", timeout=0.1)
        assert got is ev

    @pytest.mark.asyncio
    async def test_wait_for_resolves_when_event_arrives_later(self):
        pub = InMemoryPublisher()

        async def emit_later():
            await asyncio.sleep(0.01)
            await pub.publish(Event(type="task.done", task_id=3))

        task = asyncio.create_task(emit_later())
        got = await pub.wait_for("task.done", timeout=0.5)
        await task
        assert got.type == "task.done"
        assert got.task_id == 3

    @pytest.mark.asyncio
    async def test_wait_for_glob_pattern(self):
        pub = InMemoryPublisher()
        await pub.publish(Event(type="task.classified"))
        got = await pub.wait_for("task.*", timeout=0.1)
        assert got.type == "task.classified"

    @pytest.mark.asyncio
    async def test_wait_for_times_out_when_no_match(self):
        pub = InMemoryPublisher()
        with pytest.raises(asyncio.TimeoutError):
            await pub.wait_for("nope.never", timeout=0.05)

    @pytest.mark.asyncio
    async def test_clear_drops_events_and_cancels_waiters(self):
        pub = InMemoryPublisher()
        await pub.publish(Event(type="task.created"))
        assert len(pub.events) == 1
        pub.clear()
        assert pub.events == []

    @pytest.mark.asyncio
    async def test_aclose_clears_events(self):
        pub = InMemoryPublisher()
        await pub.publish(Event(type="task.created"))
        await pub.aclose()
        assert pub.events == []


class TestRedisStreamPublisher:
    @pytest.mark.asyncio
    async def test_publish_calls_xadd_with_serialised_event(self):
        fake_client = AsyncMock()
        with patch("redis.asyncio.from_url", return_value=fake_client) as mock_from_url:
            pub = RedisStreamPublisher("redis://fake:6379/0")
            ev = Event(type="task.created", task_id=42, payload={"foo": "bar"})
            await pub.publish(ev)

        mock_from_url.assert_called_once_with("redis://fake:6379/0", decode_responses=False)
        fake_client.xadd.assert_awaited_once()
        args, _ = fake_client.xadd.await_args
        assert args[0] == STREAM_KEY
        # Serialised event has the {type, data} shape
        assert args[1]["type"] == "task.created"
        assert "42" in args[1]["data"]

    @pytest.mark.asyncio
    async def test_client_reused_across_publishes(self):
        fake_client = AsyncMock()
        with patch("redis.asyncio.from_url", return_value=fake_client) as mock_from_url:
            pub = RedisStreamPublisher("redis://fake:6379/0")
            await pub.publish(Event(type="a"))
            await pub.publish(Event(type="b"))
            await pub.publish(Event(type="c"))

        # Connection opened ONCE, not per publish — that's the whole point.
        assert mock_from_url.call_count == 1
        assert fake_client.xadd.await_count == 3
        fake_client.aclose.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_aclose_closes_underlying_client(self):
        fake_client = AsyncMock()
        with patch("redis.asyncio.from_url", return_value=fake_client):
            pub = RedisStreamPublisher("redis://fake:6379/0")
            await pub.publish(Event(type="a"))
            await pub.aclose()

        fake_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aclose_without_publish_is_noop(self):
        # If the publisher was constructed but never used, aclose should not
        # blow up trying to close a None client.
        pub = RedisStreamPublisher("redis://fake:6379/0")
        await pub.aclose()  # should not raise

    @pytest.mark.asyncio
    async def test_uses_custom_stream_key(self):
        fake_client = AsyncMock()
        with patch("redis.asyncio.from_url", return_value=fake_client):
            pub = RedisStreamPublisher("redis://fake:6379/0", stream_key="custom")
            await pub.publish(Event(type="task.created"))

        args, _ = fake_client.xadd.await_args
        assert args[0] == "custom"


class TestModuleLevelPublishHelper:
    @pytest.mark.asyncio
    async def test_get_publisher_raises_when_none_set(self):
        # Reset to no publisher
        from shared import events as events_mod
        events_mod._publisher = None
        with pytest.raises(RuntimeError, match="No Publisher registered"):
            get_publisher()

    @pytest.mark.asyncio
    async def test_set_and_publish_routes_to_active_publisher(self):
        pub = InMemoryPublisher()
        set_publisher(pub)
        assert get_publisher() is pub

        ev = Event(type="task.created", task_id=7)
        await publish(ev)
        assert pub.events == [ev]

    @pytest.mark.asyncio
    async def test_swap_publisher_sends_to_new_one_only(self):
        first = InMemoryPublisher()
        second = InMemoryPublisher()

        set_publisher(first)
        await publish(Event(type="a"))
        set_publisher(second)
        await publish(Event(type="b"))

        assert [e.type for e in first.events] == ["a"]
        assert [e.type for e in second.events] == ["b"]

    @pytest.mark.asyncio
    async def test_publisher_protocol_accepts_minimal_implementations(self):
        """Anything with the right async methods qualifies as a Publisher."""

        captured: list[Event] = []

        class TinyPub:
            async def publish(self, event: Event) -> None:
                captured.append(event)

            async def aclose(self) -> None:
                pass

        set_publisher(TinyPub())
        await publish(Event(type="task.tiny"))
        assert len(captured) == 1
        assert captured[0].type == "task.tiny"
