import os

import pytest

from agent.tools.base import ToolContext


@pytest.mark.asyncio
async def test_event_sink_callable_invoked_when_set():
    received: list[dict] = []

    async def sink(event: dict) -> None:
        received.append(event)

    ctx = ToolContext(workspace="/tmp", event_sink=sink)
    assert ctx.event_sink is not None
    await ctx.event_sink({"type": "source", "url": "https://example.com"})
    assert received == [{"type": "source", "url": "https://example.com"}]


def test_event_sink_default_is_none():
    ctx = ToolContext(workspace="/tmp")
    assert ctx.event_sink is None


class TestResolve:
    """ToolContext.resolve owns the sandboxing invariant for every path-touching tool."""

    def test_relative_path_joins_under_workspace(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x")
        ctx = ToolContext(workspace=str(tmp_path))
        assert ctx.resolve("src/app.py") == str(tmp_path / "src" / "app.py")

    def test_absolute_path_inside_workspace_allowed(self, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("x")
        ctx = ToolContext(workspace=str(tmp_path))
        assert ctx.resolve(str(target)) == str(target)

    def test_dotdot_traversal_refused(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        assert ctx.resolve("../etc/passwd") is None

    def test_absolute_outside_workspace_refused(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        # /etc/passwd is not under tmp_path
        assert ctx.resolve("/etc/passwd") is None

    def test_workspace_root_itself_allowed(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        assert ctx.resolve("") == str(tmp_path)
        assert ctx.resolve(".") == str(tmp_path)

    def test_prefix_lookalike_refused(self, tmp_path):
        """Regression: workspace '/.../work' must NOT accept '/.../workshop/...'.

        The bug: a startswith check without a trailing os.sep treats 'workshop'
        as a child of 'work' because the string 'workshop' starts with 'work'.
        """
        work = tmp_path / "work"
        work.mkdir()
        workshop = tmp_path / "workshop"
        workshop.mkdir()
        secret = workshop / "secret.txt"
        secret.write_text("nope")

        ctx = ToolContext(workspace=str(work))
        assert ctx.resolve(str(secret)) is None

    def test_symlink_escape_refused(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope")
        ws = tmp_path / "ws"
        ws.mkdir()
        link = ws / "escape"
        os.symlink(outside, link)

        ctx = ToolContext(workspace=str(ws))
        # Following the symlink lands outside the workspace — must be refused.
        assert ctx.resolve("escape/secret.txt") is None


def test_tool_context_has_task_ids():
    ctx = ToolContext(workspace="/tmp/x", task_id=42, parent_task_id=7)
    assert ctx.task_id == 42
    assert ctx.parent_task_id == 7


def test_tool_context_defaults_task_ids_to_none():
    ctx = ToolContext(workspace="/tmp/x")
    assert ctx.task_id is None
    assert ctx.parent_task_id is None
