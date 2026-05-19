"""End-to-end-style wiring tests for ADR-019 repo_id / organization_id plumbing.

Group A: clone_repo callers in lifecycle code pass repo_id=task.repo_id.
Group B: run_heavy_review / run_final_review thread repo_id into boot_dev_server.
Group C: AgentLoop gains repo_id + organization_id; factory passes them from task.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Group A — clone_repo callers pass repo_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coding_clone_repo_passes_repo_id():
    """handle_coding passes repo_id=task.repo_id to clone_repo (ADR-019 T4-followup)."""
    task_id = 1
    fake_task = MagicMock()
    fake_task.id = task_id
    fake_task.repo_id = 42
    fake_task.repo_name = "org/myrepo"
    fake_task.plan = None
    fake_task.branch_name = "feat/foo"
    fake_task.created_at = "2024-01-01T00:00:00"
    fake_task.status = "coding"
    fake_task.created_by_user_id = None
    fake_task.organization_id = 7
    fake_task.title = "Test task"
    fake_task.description = "desc"
    fake_task.complexity = "simple"
    fake_task.freeform_mode = False
    fake_task.affected_routes = []
    fake_task.pr_url = None
    fake_task.output = None
    fake_task.error = None

    fake_repo = MagicMock()
    fake_repo.url = "https://github.com/org/myrepo"
    fake_repo.default_branch = "main"
    fake_repo.name = "org/myrepo"

    clone_mock = AsyncMock(return_value="/tmp/fake-ws")

    # AgentLoop mock that "succeeds"
    fake_result = MagicMock()
    fake_result.output = ""
    fake_result.error = None
    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=fake_result)

    with (
        patch("agent.lifecycle.coding.get_task", new=AsyncMock(return_value=fake_task)),
        patch("agent.lifecycle.coding.get_repo", new=AsyncMock(return_value=fake_repo)),
        patch("agent.lifecycle.coding.get_freeform_config", new=AsyncMock(return_value=None)),
        patch("agent.lifecycle.coding.clone_repo", new=clone_mock),
        patch("agent.lifecycle.coding.create_branch", new=AsyncMock()),
        patch("agent.lifecycle.coding.extract_intent", new=AsyncMock(return_value=None)),
        patch("agent.lifecycle.coding.create_agent", return_value=fake_agent),
        patch("agent.lifecycle.coding.transition_task", new=AsyncMock()),
        patch("agent.lifecycle.coding.cleanup_workspace"),
        patch("agent.lifecycle.coding.push_branch", new=AsyncMock(return_value="https://github.com/org/myrepo/compare/feat/foo")),
        patch("agent.lifecycle.coding._open_pr_and_advance", new=AsyncMock()),
        patch("agent.lifecycle.coding.home_dir_for_task", new=AsyncMock(return_value=None)),
    ):
        from agent.lifecycle.coding import handle_coding

        try:
            await handle_coding(task_id)
        except Exception:
            pass  # we only care that clone_repo was called with the right args

    # The key assertion: repo_id=42 was passed
    clone_mock.assert_called_once()
    _, kwargs = clone_mock.call_args
    assert kwargs.get("repo_id") == 42, (
        f"Expected repo_id=42, got repo_id={kwargs.get('repo_id')}. "
        f"Full kwargs: {kwargs}"
    )


# ---------------------------------------------------------------------------
# Group B — run_heavy_review and run_final_review thread repo_id into boot_dev_server
# ---------------------------------------------------------------------------


def test_run_heavy_review_accepts_repo_id_param():
    """run_heavy_review signature has repo_id keyword parameter (ADR-019 T3-followup)."""
    import inspect
    from agent.lifecycle.trio.reviewer import run_heavy_review

    sig = inspect.signature(run_heavy_review)
    assert "repo_id" in sig.parameters, (
        "run_heavy_review must accept repo_id= keyword argument"
    )
    assert sig.parameters["repo_id"].default is None


@pytest.mark.asyncio
async def test_run_heavy_review_passes_repo_id_to_boot_dev_server(tmp_path):
    """run_heavy_review(repo_id=42) passes repo_id=42 to boot_dev_server when UI routes present."""
    from agent.lifecycle.trio import reviewer as heavy_reviewer

    # Minimal workspace with required dirs
    (tmp_path / ".auto-agent").mkdir()

    # Item with a UI route so boot_dev_server is invoked
    work_item = {
        "id": "item-1",
        "title": "Add login page",
        "description": "implement login UI",
        "affected_routes": ["/login"],
        "status": "pending",
    }

    boot_mock = AsyncMock(return_value=MagicMock(state="not_running", base_url="http://localhost:3000"))

    # Smoke result: pass (so we proceed to UI inspection)
    fake_smoke = MagicMock()
    fake_smoke.verdict = "pass"
    fake_smoke.summary = "ok"
    fake_smoke.failures = []

    with (
        patch.object(heavy_reviewer, "boot_dev_server", boot_mock),
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value="diff content")),
        patch.object(heavy_reviewer, "_run_alignment_agent", AsyncMock(return_value="PASS")),
        patch.object(heavy_reviewer, "grep_diff_for_stubs", return_value=MagicMock(violations=[])),
        patch.object(heavy_reviewer, "run_smoke_agent", AsyncMock(return_value=fake_smoke)),
        patch.object(heavy_reviewer, "read_gate_file", MagicMock(return_value="")),
        patch.object(heavy_reviewer, "_write_review_json", MagicMock()),
        patch.object(heavy_reviewer, "is_ui_route", return_value=True),
        patch.object(heavy_reviewer, "infer_routes_from_diff", return_value=[]),
    ):
        try:
            await heavy_reviewer.run_heavy_review(
                item=work_item,
                workspace_root=str(tmp_path),
                base_sha="abc123",
                grill_output="",
                repo_id=42,
            )
        except Exception:
            pass  # we only care that boot_dev_server got the right args

    # boot_dev_server must have been called (item has /login which is_ui_route=True)
    assert boot_mock.called, "boot_dev_server should have been called for UI route /login"
    _, kwargs = boot_mock.call_args
    assert kwargs.get("repo_id") == 42, (
        f"Expected boot_dev_server(repo_id=42), got {kwargs}"
    )


def test_run_final_review_accepts_repo_id_param():
    """run_final_review signature has repo_id keyword parameter (ADR-019 T3-followup)."""
    import inspect
    from agent.lifecycle.trio.final_reviewer import run_final_review

    sig = inspect.signature(run_final_review)
    assert "repo_id" in sig.parameters, (
        "run_final_review must accept repo_id= keyword argument"
    )
    assert sig.parameters["repo_id"].default is None


@pytest.mark.asyncio
async def test_run_final_review_passes_repo_id_to_smoke_and_ui(tmp_path):
    """run_final_review(repo_id=42) threads repo_id into _smoke_and_ui."""
    from agent.lifecycle.trio import final_reviewer

    # Minimal workspace layout
    (tmp_path / ".auto-agent").mkdir()

    captured_smoke_and_ui_kwargs: list[dict] = []

    async def fake_smoke_and_ui(**kwargs):
        captured_smoke_and_ui_kwargs.append(kwargs)
        return [], "smoke: pass"

    with (
        patch.object(final_reviewer, "_smoke_and_ui", fake_smoke_and_ui),
        patch.object(final_reviewer, "_load_integrated_diff", AsyncMock(return_value="diff")),
        patch.object(final_reviewer, "_read_design", MagicMock(return_value="design")),
        patch.object(final_reviewer, "_read_backlog_items", MagicMock(return_value=[])),
        patch.object(final_reviewer, "_read_reviews", MagicMock(return_value=[])),
        patch.object(final_reviewer, "_union_affected_routes", MagicMock(return_value=[])),
        patch.object(final_reviewer, "_run_final_review_agent", AsyncMock(return_value="")),
        patch.object(final_reviewer, "read_gate_file", MagicMock(
            return_value={"verdict": "passed", "gaps": [], "summary": "ok"}
        )),
        patch.object(final_reviewer, "_write_final_review_json", MagicMock()),
    ):
        try:
            await final_reviewer.run_final_review(
                workspace_root=str(tmp_path),
                parent_task_id=99,
                repo_id=42,
            )
        except Exception:
            pass  # we only care that _smoke_and_ui got the right args

    assert len(captured_smoke_and_ui_kwargs) >= 1, "_smoke_and_ui was never called"
    assert captured_smoke_and_ui_kwargs[0].get("repo_id") == 42, (
        f"Expected _smoke_and_ui(repo_id=42), got {captured_smoke_and_ui_kwargs[0]}"
    )


# ---------------------------------------------------------------------------
# Group C — AgentLoop gains repo_id + organization_id; ToolContext gets them
# ---------------------------------------------------------------------------


def test_agent_loop_stores_repo_id_and_organization_id():
    """AgentLoop constructor stores repo_id and organization_id as instance fields."""
    from agent.loop import AgentLoop

    loop = AgentLoop(
        provider=MagicMock(),
        tools=MagicMock(),
        context_manager=MagicMock(),
        workspace="/tmp/test",
        repo_id=42,
        organization_id=7,
    )
    assert loop._repo_id == 42
    assert loop._organization_id == 7


def test_agent_loop_defaults_repo_id_to_none():
    """AgentLoop defaults repo_id and organization_id to None when not provided."""
    from agent.loop import AgentLoop

    loop = AgentLoop(
        provider=MagicMock(),
        tools=MagicMock(),
        context_manager=MagicMock(),
        workspace="/tmp/test",
    )
    assert loop._repo_id is None
    assert loop._organization_id is None


@pytest.mark.asyncio
async def test_agent_loop_passes_repo_id_to_tool_context():
    """AgentLoop.run() constructs ToolContext with repo_id and organization_id."""
    from agent.loop import AgentLoop
    from agent.tools.base import ToolContext

    captured_contexts: list[ToolContext] = []

    # Minimal provider: returns empty result so the loop terminates.
    fake_response = MagicMock()
    fake_response.content = "done"
    fake_response.tool_calls = []
    fake_response.stop_reason = "end_turn"
    fake_response.usage = MagicMock(input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0)

    fake_provider = AsyncMock()
    fake_provider.complete = AsyncMock(return_value=fake_response)

    fake_tools = MagicMock()
    fake_tools.definitions = MagicMock(return_value=[])
    fake_tools.secret_tools = MagicMock(return_value=[])

    fake_context = MagicMock()
    fake_context.prepare = AsyncMock(return_value=("system prompt", []))
    fake_context.should_summarize = MagicMock(return_value=False)
    fake_context.workspace_state = MagicMock()
    fake_context.workspace_state.mark_tested = MagicMock()

    original_tool_context_init = ToolContext.__init__

    def capture_init(self, *args, **kwargs):
        original_tool_context_init(self, *args, **kwargs)
        captured_contexts.append(self)

    with patch.object(ToolContext, "__init__", capture_init):
        loop = AgentLoop(
            provider=fake_provider,
            tools=fake_tools,
            context_manager=fake_context,
            workspace="/tmp/test-ws",
            max_turns=1,
            repo_id=42,
            organization_id=7,
        )
        try:
            await loop.run("hello")
        except Exception:
            pass

    assert len(captured_contexts) >= 1, "ToolContext was never constructed"
    ctx = captured_contexts[0]
    assert ctx.repo_id == 42, f"Expected repo_id=42, got {ctx.repo_id}"
    assert ctx.organization_id == 7, f"Expected organization_id=7, got {ctx.organization_id}"
