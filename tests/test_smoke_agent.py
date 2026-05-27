"""Smoke agent — dedicated runtime-verification agent. ADR-015 §3 / Phase 7.8.

The smoke agent is invoked after the builder finishes an item. Its
job is to actually exercise the code in the workspace (install deps,
boot the dev server, hit routes, run the project's test suite) and
write ``.auto-agent/smoke_result.json`` via the ``submit-smoke-result``
skill. The heavy reviewer and final reviewer consume that file —
``verdict != "pass"`` blocks the gate. There is no vacuous-pass branch.

Five behaviours pinned here:

1. The agent writing ``verdict="pass"`` round-trips into a passing
   :class:`SmokeAgentResult`.
2. ``verdict="fail"`` round-trips with the failures preserved.
3. Missing ``smoke_result.json`` after the agent runs → automatic fail
   with a clear reason (the agent shirked its duty).
4. ``verdict="skipped"`` is rewritten to fail by the orchestrator —
   skipping is not a pass.
5. The agent prompt carries the workspace path, the item, the diff,
   the design text, and the workspace's existing ``auto-agent.smoke.yml``
   contents if present.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _stub_auto_route_checks(request):
    """Stub out the Python-side dev-server boot/curl by default.

    The real ``_run_auto_route_checks`` (added with the auto-agent-owned
    smoke runner refactor) actually spawns ``python3 run.py`` etc. when
    a workspace has an ``auto-agent.smoke.yml`` or detectable boot
    command — that's the whole point of the refactor. Most tests focus
    on the LLM-driven verdict round-trip and don't want a real
    subprocess. Tests that exercise the helper itself opt out via
    ``@pytest.mark.real_route_checks``.
    """
    if request.node.get_closest_marker("real_route_checks") is not None:
        yield
        return

    from agent.lifecycle.trio import smoke_agent

    async def _noop(**kwargs):
        return ("(test stub: no auto-route checks)", [])

    with patch.object(smoke_agent, "_run_auto_route_checks", new=_noop):
        yield


# ---------------------------------------------------------------------------
# 1. Agent writes verdict=pass → SmokeAgentResult(verdict="pass").
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_verdict_round_trips(tmp_path: Path) -> None:
    from agent.lifecycle.trio import smoke_agent

    payload = {
        "schema_version": "1",
        "verdict": "pass",
        "summary": "Installed deps, booted dev server, hit 3 routes — all 2xx.",
        "attempts": [
            {
                "step": "install",
                "command": "pip install -e .",
                "exit_code": 0,
                "ok": True,
                "output_preview": "",
            },
            {
                "step": "boot",
                "command": "python3 run.py",
                "exit_code": 0,
                "ok": True,
                "output_preview": "",
            },
            {
                "step": "route /health",
                "command": "curl localhost:8000/health",
                "exit_code": 0,
                "ok": True,
                "output_preview": '{"ok":true}',
            },
        ],
        "failures": [],
        "proposed_smoke_yml": "",
    }

    async def fake_run(*args, **kwargs):
        (tmp_path / ".auto-agent").mkdir(exist_ok=True)
        (tmp_path / ".auto-agent" / "smoke_result.json").write_text(json.dumps(payload))
        return MagicMock(output="done")

    fake_agent = MagicMock()
    fake_agent.run = fake_run

    with patch.object(smoke_agent, "create_agent", return_value=fake_agent):
        result = await smoke_agent.run_smoke_agent(
            workspace_root=str(tmp_path),
            item={"id": "T1", "title": "x", "description": "y", "affected_routes": ["/health"]},
            design="# design",
            diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
        )

    assert result.verdict == "pass"
    assert "booted dev server" in result.summary
    assert len(result.attempts) == 3
    assert result.failures == []


# ---------------------------------------------------------------------------
# 2. Agent writes verdict=fail → SmokeAgentResult(verdict="fail").
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_verdict_round_trips_with_failures(tmp_path: Path) -> None:
    from agent.lifecycle.trio import smoke_agent

    payload = {
        "schema_version": "1",
        "verdict": "fail",
        "summary": "pytest blew up at first import.",
        "attempts": [
            {
                "step": "pytest",
                "command": "pytest -q",
                "exit_code": 1,
                "ok": False,
                "output_preview": "ImportError: no module x",
            },
        ],
        "failures": ["pytest exit_code=1; ImportError on shared.frobnicator"],
        "proposed_smoke_yml": "",
    }

    async def fake_run(*args, **kwargs):
        (tmp_path / ".auto-agent").mkdir(exist_ok=True)
        (tmp_path / ".auto-agent" / "smoke_result.json").write_text(json.dumps(payload))
        return MagicMock(output="done")

    fake_agent = MagicMock()
    fake_agent.run = fake_run

    with patch.object(smoke_agent, "create_agent", return_value=fake_agent):
        result = await smoke_agent.run_smoke_agent(
            workspace_root=str(tmp_path),
            item={"id": "T1", "title": "x", "description": "y", "affected_routes": []},
            design="# design",
            diff="",
        )

    assert result.verdict == "fail"
    assert result.failures == ["pytest exit_code=1; ImportError on shared.frobnicator"]


# ---------------------------------------------------------------------------
# 3. Missing smoke_result.json → automatic fail.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file_after_agent_run_is_fail(tmp_path: Path) -> None:
    from agent.lifecycle.trio import smoke_agent

    async def fake_run(*args, **kwargs):
        # Agent never writes the file.
        return MagicMock(output="I read the code, looks good!")

    fake_agent = MagicMock()
    fake_agent.run = fake_run

    with patch.object(smoke_agent, "create_agent", return_value=fake_agent):
        result = await smoke_agent.run_smoke_agent(
            workspace_root=str(tmp_path),
            item={"id": "T1", "title": "x", "description": "y", "affected_routes": []},
            design="",
            diff="",
        )

    assert result.verdict == "fail"
    assert "smoke_result.json" in result.summary.lower()


# ---------------------------------------------------------------------------
# 4. Skipped verdict is rewritten to fail.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skipped_verdict_is_failure(tmp_path: Path) -> None:
    from agent.lifecycle.trio import smoke_agent

    payload = {
        "schema_version": "1",
        "verdict": "skipped",
        "summary": "I felt lazy.",
        "attempts": [],
        "failures": [],
        "proposed_smoke_yml": "",
    }

    async def fake_run(*args, **kwargs):
        (tmp_path / ".auto-agent").mkdir(exist_ok=True)
        (tmp_path / ".auto-agent" / "smoke_result.json").write_text(json.dumps(payload))
        return MagicMock(output="done")

    fake_agent = MagicMock()
    fake_agent.run = fake_run

    with patch.object(smoke_agent, "create_agent", return_value=fake_agent):
        result = await smoke_agent.run_smoke_agent(
            workspace_root=str(tmp_path),
            item={"id": "T1", "title": "x", "description": "y", "affected_routes": []},
            design="",
            diff="something.py changed",
        )

    assert result.verdict == "fail"
    assert (
        "skip" in result.summary.lower()
        or "skip" in (result.failures[0] if result.failures else "").lower()
    )


# ---------------------------------------------------------------------------
# 5. Prompt carries workspace context.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_includes_item_diff_design_and_smoke_yml(tmp_path: Path) -> None:
    from agent.lifecycle.trio import smoke_agent

    # Pre-existing smoke config in the workspace.
    (tmp_path / "auto-agent.smoke.yml").write_text(
        "boot_command: python3 run.py\nhealth_check_url: http://127.0.0.1:8000/health\n"
    )

    received_prompts: list[str] = []

    async def fake_run(prompt, *args, **kwargs):
        received_prompts.append(prompt)
        # Write a passing result so the test focuses on the prompt.
        (tmp_path / ".auto-agent").mkdir(exist_ok=True)
        (tmp_path / ".auto-agent" / "smoke_result.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "verdict": "pass",
                    "summary": "ok",
                    "attempts": [],
                    "failures": [],
                }
            )
        )
        return MagicMock(output="done")

    fake_agent = MagicMock()
    fake_agent.run = fake_run

    with patch.object(smoke_agent, "create_agent", return_value=fake_agent):
        await smoke_agent.run_smoke_agent(
            workspace_root=str(tmp_path),
            item={
                "id": "T7",
                "title": "Add login route",
                "description": "POST /api/login",
                "affected_routes": ["/api/login"],
            },
            design="# Design\nLogin endpoint.",
            diff="+def login():\n+    return jsonify({'ok': True})\n",
        )

    assert len(received_prompts) == 1
    prompt = received_prompts[0]
    # Item context surfaced.
    assert "T7" in prompt
    assert "Add login route" in prompt
    # Design surfaced.
    assert "Login endpoint" in prompt
    # Affected route surfaced.
    assert "/api/login" in prompt
    # smoke.yml contents surfaced (so the agent doesn't have to re-discover the boot command).
    assert "python3 run.py" in prompt
    # Diff surfaced.
    assert "def login" in prompt


# ---------------------------------------------------------------------------
# 6. Bad schema_version is treated as a failure (corrupt artefact).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_schema_version_is_fail(tmp_path: Path) -> None:
    from agent.lifecycle.trio import smoke_agent

    payload = {
        "schema_version": "999",
        "verdict": "pass",
        "summary": "ok",
        "attempts": [],
        "failures": [],
    }

    async def fake_run(*args, **kwargs):
        (tmp_path / ".auto-agent").mkdir(exist_ok=True)
        (tmp_path / ".auto-agent" / "smoke_result.json").write_text(json.dumps(payload))
        return MagicMock(output="done")

    fake_agent = MagicMock()
    fake_agent.run = fake_run

    with patch.object(smoke_agent, "create_agent", return_value=fake_agent):
        result = await smoke_agent.run_smoke_agent(
            workspace_root=str(tmp_path),
            item={"id": "T1", "title": "x", "description": "y", "affected_routes": []},
            design="",
            diff="",
        )

    assert result.verdict == "fail"


# ---------------------------------------------------------------------------
# 7. Reviewer integration — smoke-agent fail blocks the per-item review.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heavy_review_fails_when_smoke_agent_fails(tmp_path: Path) -> None:
    """The vacuous-pass loophole at reviewer.py:629 is closed.

    The fix: when smoke-agent returns ``verdict="fail"``, ``run_heavy_review``
    must short-circuit with ``verdict="fail"`` regardless of whether any
    routes were inferred.
    """

    from agent.lifecycle.trio import reviewer, smoke_agent

    async def fake_diff(*args, **kwargs):
        return "+def thing(): pass\n"

    async def fake_alignment(*args, **kwargs):
        return "PASS: looks correct against spec"

    fake_smoke = AsyncMock(
        return_value=smoke_agent.SmokeAgentResult(
            verdict="fail",
            summary="dev server never bound to its port",
            attempts=[],
            failures=["boot timed out after 60s"],
        )
    )

    with (
        patch.object(reviewer, "_load_item_diff", new=fake_diff),
        patch.object(reviewer, "_run_alignment_agent", new=fake_alignment),
        patch.object(reviewer, "run_smoke_agent", new=fake_smoke),
    ):
        result = await reviewer.run_heavy_review(
            item={"id": "T1", "title": "x", "description": "y", "affected_routes": []},
            workspace_root=str(tmp_path),
            base_sha="HEAD~1",
        )

    assert result.verdict == "fail"
    assert "smoke" in result.smoke.lower() or "boot" in result.reason.lower()


# ---------------------------------------------------------------------------
# 8. Reviewer integration — even with zero routes, smoke MUST run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heavy_review_always_invokes_smoke_agent(tmp_path: Path) -> None:
    """No more 'no routes inferred = skip smoke' (the task-1 false-positive bug).

    Even when the item declares no routes and the diff infers none, the
    smoke agent must still be invoked. The agent itself decides what
    'smoke testing this change' means — at minimum, run the project's
    test suite.
    """

    from agent.lifecycle.trio import reviewer, smoke_agent

    async def fake_diff(*args, **kwargs):
        return "+# pure refactor — no routes touched\n"

    async def fake_alignment(*args, **kwargs):
        return "PASS"

    fake_smoke = AsyncMock(
        return_value=smoke_agent.SmokeAgentResult(
            verdict="pass",
            summary="ran pytest -q; 1452 passed",
            attempts=[],
            failures=[],
        )
    )

    with (
        patch.object(reviewer, "_load_item_diff", new=fake_diff),
        patch.object(reviewer, "_run_alignment_agent", new=fake_alignment),
        patch.object(reviewer, "run_smoke_agent", new=fake_smoke),
    ):
        await reviewer.run_heavy_review(
            item={"id": "T1", "title": "refactor", "description": "y", "affected_routes": []},
            workspace_root=str(tmp_path),
            base_sha="HEAD~1",
        )

    fake_smoke.assert_awaited_once()


# ---------------------------------------------------------------------------
# 7. Auto-agent-owned dev-server lifecycle (refactor 2026-05-27).
# ---------------------------------------------------------------------------


@pytest.mark.real_route_checks
@pytest.mark.asyncio
async def test_run_auto_route_checks_no_routes_no_smoke_yml(tmp_path: Path) -> None:
    """No routes + no smoke.yml = skip boot, no failures."""
    from agent.lifecycle.trio import smoke_agent

    block, failures = await smoke_agent._run_auto_route_checks(
        workspace_root=str(tmp_path),
        item={"id": "T1", "affected_routes": []},
        diff="",
        repo_id=None,
    )
    assert failures == []
    assert "no routes" in block.lower() or "did not boot" in block.lower()


@pytest.mark.real_route_checks
@pytest.mark.asyncio
async def test_run_auto_route_checks_boot_failed_yields_failure(tmp_path: Path) -> None:
    """When boot_dev_server returns state='failed', the helper synthesises
    a definitive failure so run_smoke_agent can short-circuit claude."""
    from agent.lifecycle.trio import smoke_agent
    from agent.lifecycle.verify_primitives import ServerHandle

    failed_handle = ServerHandle(state="failed", failure_reason="health_check_timeout")

    async def fake_boot(*, workspace, repo_id):
        return failed_handle

    with patch("agent.lifecycle.verify_primitives.boot_dev_server", new=fake_boot):
        block, failures = await smoke_agent._run_auto_route_checks(
            workspace_root=str(tmp_path),
            item={"id": "T1", "affected_routes": ["/health"]},
            diff="",
            repo_id=None,
        )
    assert failures, "boot failure must yield at least one failure entry"
    assert "boot failed" in block.lower() or "health_check_timeout" in block.lower()


@pytest.mark.asyncio
async def test_run_smoke_agent_short_circuits_on_boot_failure(tmp_path: Path) -> None:
    """When the auto-route helper reports failures, run_smoke_agent must
    NOT invoke claude — the diff demonstrably doesn't run."""
    from agent.lifecycle.trio import smoke_agent

    async def failing_routes(**kwargs):
        return ("BOOT FAILED: synthetic", ["dev server failed to boot: synthetic"])

    create_agent_called: list[bool] = []

    def boom(*a, **kw):
        create_agent_called.append(True)
        raise AssertionError("create_agent must NOT be called on boot failure")

    # The autouse stub above replaces _run_auto_route_checks with a noop;
    # override it here with the failing version.
    with (
        patch.object(smoke_agent, "_run_auto_route_checks", new=failing_routes),
        patch.object(smoke_agent, "create_agent", side_effect=boom),
    ):
        result = await smoke_agent.run_smoke_agent(
            workspace_root=str(tmp_path),
            item={"id": "T1", "affected_routes": ["/health"]},
            design="",
            diff="",
        )

    assert result.verdict == "fail"
    assert "synthetic" in result.summary
    assert create_agent_called == []


@pytest.mark.asyncio
async def test_run_smoke_agent_embeds_route_check_block_in_prompt(
    tmp_path: Path,
) -> None:
    """The route-check block produced by the helper must be embedded in
    the prompt handed to claude — that's how the LLM learns the auto-
    agent already checked the routes."""
    from agent.lifecycle.trio import smoke_agent

    async def stub_routes(**kwargs):
        return ("ROUTE_CHECK_MARKER_xyz", [])

    received: list[str] = []

    async def fake_run(prompt, *args, **kwargs):
        received.append(prompt)
        (tmp_path / ".auto-agent").mkdir(exist_ok=True)
        (tmp_path / ".auto-agent" / "smoke_result.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "verdict": "pass",
                    "summary": "ok",
                    "attempts": [],
                    "failures": [],
                }
            )
        )
        return MagicMock(output="done")

    fake_agent = MagicMock()
    fake_agent.run = fake_run

    with (
        patch.object(smoke_agent, "_run_auto_route_checks", new=stub_routes),
        patch.object(smoke_agent, "create_agent", return_value=fake_agent),
    ):
        result = await smoke_agent.run_smoke_agent(
            workspace_root=str(tmp_path),
            item={"id": "T1", "affected_routes": ["/api/login"]},
            design="",
            diff="",
        )

    assert result.verdict == "pass"
    assert len(received) == 1
    assert "ROUTE_CHECK_MARKER_xyz" in received[0]


@pytest.mark.real_route_checks
@pytest.mark.asyncio
async def test_run_smoke_agent_handle_teardown_always_runs(tmp_path: Path) -> None:
    """``ServerHandle.teardown`` must run even when ``exercise_routes`` raises.
    The teardown SIGKILLs the process group — skipping it on the error
    path is exactly what wedged task 28."""
    from agent.lifecycle.trio import smoke_agent
    from agent.lifecycle.verify_primitives import ServerHandle

    teardown_calls: list[int] = []

    class _FakeHandle(ServerHandle):
        async def teardown(self):
            teardown_calls.append(1)

    fake_handle = _FakeHandle(state="running", base_url="http://127.0.0.1:9999", pid=12345)

    async def fake_boot(*, workspace, repo_id):
        return fake_handle

    async def boom_routes(routes, *, handle):
        raise RuntimeError("synthetic exercise_routes crash")

    with (
        patch("agent.lifecycle.verify_primitives.boot_dev_server", new=fake_boot),
        patch("agent.lifecycle.verify_primitives.exercise_routes", new=boom_routes),
    ):
        block, failures = await smoke_agent._run_auto_route_checks(
            workspace_root=str(tmp_path),
            item={"id": "T1", "affected_routes": ["/health"]},
            diff="",
            repo_id=None,
        )

    assert teardown_calls == [1], "teardown must run on the error path"
    # The crash is surfaced in the block but does NOT synthesise a hard
    # failure — claude can still run tests/build/typecheck.
    assert "crashed" in block.lower()
    assert failures == []
