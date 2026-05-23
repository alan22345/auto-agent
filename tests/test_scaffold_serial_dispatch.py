"""Regression tests for bug 17 — child trios under a SCAFFOLD parent must
build serially.

User contract (handover doc, harpoon run 2026-05-23): "only one child trio
writing code at any moment. Parallelism across children is not correct
functionality." The trio dispatcher has no per-scaffold lock and the
per-repo concurrency cap doesn't count ``TRIO_EXECUTING`` as active, so
publishing ``task_created`` for every child at the end of
``dispatch_children.run`` raced them all into ``TRIO_EXECUTING`` at once.

Fix shape:
  * ``dispatch_children.run`` calls ``_publish_next_scaffold_child`` instead
    of looping ``publish(task_created(...))`` per child; the helper picks
    the lowest-id ``INTAKE`` child and only publishes if no sibling is
    already in flight.
  * The fan-in handler ``_maybe_advance_scaffold_parent_on_child_finish``
    in ``run.py`` calls the same helper on every terminal child event;
    only when *no* pending sibling remains does it advance the parent
    to ``AWAITING_FINAL_VERIFICATION``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from shared.models import TaskStatus


class _FakeChild:
    """Minimal stand-in for a ``Task`` row used by the helper."""

    def __init__(self, child_id: int, status: TaskStatus) -> None:
        self.id = child_id
        self.status = status
        self.parent_task_id = 1


def _make_fake_session(children: list[_FakeChild]):
    @asynccontextmanager
    async def factory():
        class _Result:
            def scalars(self):
                class _S:
                    def all(self):
                        return list(children)

                return _S()

        class _Session:
            async def execute(self, *_a, **_kw):
                return _Result()

        yield _Session()

    return factory


@pytest.mark.asyncio
async def test_publish_next_picks_lowest_id_intake_child() -> None:
    """When several INTAKE children exist, the helper publishes the lowest id."""

    from agent.lifecycle.scaffold import dispatch_children as mod

    children = [
        _FakeChild(102, TaskStatus.INTAKE),
        _FakeChild(100, TaskStatus.INTAKE),
        _FakeChild(101, TaskStatus.INTAKE),
    ]
    publish_mock = AsyncMock()
    with (
        patch.object(mod, "async_session", _make_fake_session(children)),
        patch.object(mod, "publish", publish_mock),
    ):
        chosen = await mod._publish_next_scaffold_child(parent_id=1)

    assert chosen == 100
    publish_mock.assert_awaited_once()
    event = publish_mock.await_args.args[0]
    assert event.task_id == 100


@pytest.mark.asyncio
async def test_publish_next_skips_when_sibling_in_flight() -> None:
    """A sibling already in TRIO_EXECUTING (or any non-terminal non-INTAKE)
    blocks the helper. Serial-dispatch contract: one at a time."""

    from agent.lifecycle.scaffold import dispatch_children as mod

    children = [
        _FakeChild(100, TaskStatus.TRIO_EXECUTING),
        _FakeChild(101, TaskStatus.INTAKE),
    ]
    publish_mock = AsyncMock()
    with (
        patch.object(mod, "async_session", _make_fake_session(children)),
        patch.object(mod, "publish", publish_mock),
    ):
        chosen = await mod._publish_next_scaffold_child(parent_id=1)

    assert chosen is None
    publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_next_skips_when_no_intake_left() -> None:
    """All children terminal → nothing to dispatch; helper is a no-op."""

    from agent.lifecycle.scaffold import dispatch_children as mod

    children = [
        _FakeChild(100, TaskStatus.DONE),
        _FakeChild(101, TaskStatus.FAILED),
        _FakeChild(102, TaskStatus.BLOCKED),
    ]
    publish_mock = AsyncMock()
    with (
        patch.object(mod, "async_session", _make_fake_session(children)),
        patch.object(mod, "publish", publish_mock),
    ):
        chosen = await mod._publish_next_scaffold_child(parent_id=1)

    assert chosen is None
    publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_next_treats_terminal_siblings_as_not_in_flight() -> None:
    """Terminal siblings (DONE/FAILED/BLOCKED) must NOT block the next
    pending child — otherwise a single failure would strand the rest."""

    from agent.lifecycle.scaffold import dispatch_children as mod

    children = [
        _FakeChild(100, TaskStatus.DONE),
        _FakeChild(101, TaskStatus.BLOCKED),
        _FakeChild(102, TaskStatus.INTAKE),
        _FakeChild(103, TaskStatus.INTAKE),
    ]
    publish_mock = AsyncMock()
    with (
        patch.object(mod, "async_session", _make_fake_session(children)),
        patch.object(mod, "publish", publish_mock),
    ):
        chosen = await mod._publish_next_scaffold_child(parent_id=1)

    assert chosen == 102
    publish_mock.assert_awaited_once()
    event = publish_mock.await_args.args[0]
    assert event.task_id == 102
