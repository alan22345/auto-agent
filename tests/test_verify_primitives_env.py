"""Tests for boot_dev_server env handling — ADR-019 T3.

Covers:
- filtered host env is used (orchestrator-scope keys stripped)
- project secrets injected when repo_id provided
- project secrets win on collision with benign host vars
- PORT is always set last
- repo_id=None → no repo_secrets call
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_smoke_yml(directory: Path, boot_command: str = "python3 server.py") -> None:
    """Write a minimal auto-agent.smoke.yml to the given directory."""
    yml = (
        f"boot_command: {boot_command}\n"
        "health_check_url: http://127.0.0.1:9999/\n"
        "boot_timeout: 5\n"
    )
    (directory / "auto-agent.smoke.yml").write_text(yml)


# ---------------------------------------------------------------------------
# repo_id=None → no repo_secrets.get_all_for_boot call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_dev_server_no_repo_id_skips_secret_lookup(tmp_path):
    """When repo_id is None, get_all_for_boot must never be called."""
    _make_smoke_yml(tmp_path)

    with (
        patch(
            "agent.lifecycle.verify_primitives.asyncio.create_subprocess_shell",
        ) as mock_proc,
        patch(
            "agent.lifecycle.verify_primitives._wait_for_health",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "shared.repo_secrets.get_all_for_boot",
            new=AsyncMock(),
        ) as mock_secrets,
    ):
        proc = MagicMock()
        proc.pid = 12345
        mock_proc.return_value = proc
        with patch("os.getpgid", return_value=12345):
            from agent.lifecycle.verify_primitives import boot_dev_server

            await boot_dev_server(workspace=str(tmp_path), repo_id=None)

    mock_secrets.assert_not_called()


# ---------------------------------------------------------------------------
# repo_id provided → repo_secrets injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_dev_server_with_repo_id_calls_get_all_for_boot(tmp_path):
    """When repo_id is set, get_all_for_boot is called with correct args."""
    _make_smoke_yml(tmp_path)

    fake_repo = MagicMock()
    fake_repo.organization_id = 99

    captured_env: dict = {}

    async def capture_proc(cmd, *, cwd, env, stdout, stderr, preexec_fn):
        captured_env.update(env)
        proc = MagicMock()
        proc.pid = 12345
        return proc

    with (
        patch(
            "agent.lifecycle.verify_primitives.asyncio.create_subprocess_shell",
            side_effect=capture_proc,
        ),
        patch(
            "agent.lifecycle.verify_primitives._wait_for_health",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "agent.lifecycle.verify_primitives.async_session",
        ) as mock_session_cm,
        patch(
            "agent.lifecycle.verify_primitives.repo_secrets.get_all_for_boot",
            new=AsyncMock(return_value={"STRIPE_API_KEY": "sk_test_abc"}),
        ) as mock_secrets,
    ):
        # Set up the async context manager for async_session
        mock_session = AsyncMock()
        mock_session_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        # scalar_one_or_none returns the fake repo
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=fake_repo))
        )

        with patch("os.getpgid", return_value=12345):
            from agent.lifecycle.verify_primitives import boot_dev_server

            await boot_dev_server(workspace=str(tmp_path), repo_id=7)

    mock_secrets.assert_called_once()
    call_kwargs = mock_secrets.call_args
    assert call_kwargs.args[0] == 7  # repo_id positional
    assert call_kwargs.kwargs.get("organization_id") == 99


# ---------------------------------------------------------------------------
# Project secrets land in the subprocess env
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_dev_server_project_secrets_in_subprocess_env(tmp_path):
    """Project secrets returned by get_all_for_boot appear in subprocess env."""
    _make_smoke_yml(tmp_path)

    fake_repo = MagicMock()
    fake_repo.organization_id = 5

    captured_env: dict = {}

    async def capture_proc(cmd, *, cwd, env, stdout, stderr, preexec_fn):
        captured_env.update(env)
        proc = MagicMock()
        proc.pid = 99
        return proc

    with (
        patch(
            "agent.lifecycle.verify_primitives.asyncio.create_subprocess_shell",
            side_effect=capture_proc,
        ),
        patch(
            "agent.lifecycle.verify_primitives._wait_for_health",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "agent.lifecycle.verify_primitives.async_session",
        ) as mock_session_cm,
        patch(
            "agent.lifecycle.verify_primitives.repo_secrets.get_all_for_boot",
            new=AsyncMock(return_value={"MY_PROJECT_KEY": "secret-value-123"}),
        ),
    ):
        mock_session = AsyncMock()
        mock_session_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=fake_repo))
        )

        with patch("os.getpgid", return_value=99):
            from agent.lifecycle.verify_primitives import boot_dev_server

            await boot_dev_server(workspace=str(tmp_path), repo_id=3)

    assert captured_env.get("MY_PROJECT_KEY") == "secret-value-123"


# ---------------------------------------------------------------------------
# Orchestrator-scope keys must NOT leak through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_dev_server_strips_orchestrator_keys(tmp_path, monkeypatch):
    """reserved_env_keys() entries must not appear in the subprocess env."""
    _make_smoke_yml(tmp_path)

    # Make sure SECRETS_PASSPHRASE is in the environment before the call.
    monkeypatch.setenv("SECRETS_PASSPHRASE", "super-secret-passphrase")

    captured_env: dict = {}

    async def capture_proc(cmd, *, cwd, env, stdout, stderr, preexec_fn):
        captured_env.update(env)
        proc = MagicMock()
        proc.pid = 55
        return proc

    with (
        patch(
            "agent.lifecycle.verify_primitives.asyncio.create_subprocess_shell",
            side_effect=capture_proc,
        ),
        patch(
            "agent.lifecycle.verify_primitives._wait_for_health",
            new=AsyncMock(return_value=True),
        ),
        patch("os.getpgid", return_value=55),
    ):
        from agent.lifecycle.verify_primitives import boot_dev_server

        await boot_dev_server(workspace=str(tmp_path), repo_id=None)

    assert "SECRETS_PASSPHRASE" not in captured_env, (
        "SECRETS_PASSPHRASE must not leak into the subprocess env"
    )
    assert "ANTHROPIC_API_KEY" not in captured_env


# ---------------------------------------------------------------------------
# PORT is always set last — project secrets cannot override it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_dev_server_port_set_last(tmp_path):
    """PORT in subprocess env must equal the allocated port, not a project secret."""
    _make_smoke_yml(tmp_path, boot_command="npm run dev")
    # smoke.yml doesn't auto-detect — remove it to force the auto-detect path
    # (which allocates a port and exports it as $PORT).
    (tmp_path / "auto-agent.smoke.yml").unlink()
    (tmp_path / "package.json").write_text('{"scripts": {"dev": "echo hi"}}')

    fake_repo = MagicMock()
    fake_repo.organization_id = 1

    captured_env: dict = {}

    async def capture_proc(cmd, *, cwd, env, stdout, stderr, preexec_fn):
        captured_env.update(env)
        proc = MagicMock()
        proc.pid = 77
        return proc

    with (
        patch(
            "agent.lifecycle.verify_primitives.asyncio.create_subprocess_shell",
            side_effect=capture_proc,
        ),
        patch(
            "agent.lifecycle.verify_primitives._wait_for_health",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "agent.lifecycle.verify_primitives.async_session",
        ) as mock_session_cm,
        patch(
            "agent.lifecycle.verify_primitives.repo_secrets.get_all_for_boot",
            # Project tries to declare PORT — must be overridden.
            new=AsyncMock(return_value={"PORT": "1234"}),
        ),
        patch(
            "agent.lifecycle.verify_primitives._allocate_port",
            return_value=54321,
        ),
    ):
        mock_session = AsyncMock()
        mock_session_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=fake_repo))
        )

        with patch("os.getpgid", return_value=77):
            from agent.lifecycle.verify_primitives import boot_dev_server

            await boot_dev_server(workspace=str(tmp_path), repo_id=2)

    # The allocated port wins, not the project-declared "1234".
    assert captured_env.get("PORT") == "54321", (
        f"PORT should be the allocated port (54321), got: {captured_env.get('PORT')!r}"
    )


# ---------------------------------------------------------------------------
# ADR-019 T3 — callers wire repo_id through to boot_dev_server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_reviewer_correctness_passes_repo_id_to_boot_dev_server(tmp_path):
    """_run_correctness_review forwards task.repo_id to boot_dev_server (ADR-019 T3)."""
    from types import SimpleNamespace

    from agent.lifecycle import pr_reviewer

    task = SimpleNamespace(
        id=1,
        pr_url="http://gh/pr/1",
        base_branch="main",
        branch_name="feat/x",
        title="t",
        description="d",
        repo_id=42,
    )

    handle = MagicMock()
    handle.state = "disabled"
    handle.teardown = AsyncMock()

    boot_mock = AsyncMock(return_value=handle)

    diff = (
        "diff --git a/api/routes.py b/api/routes.py\n"
        "--- a/api/routes.py\n"
        "+++ b/api/routes.py\n"
        "@@ -1,3 +1,5 @@\n"
        " from fastapi import APIRouter\n"
        "+@router.get('/widgets')\n"
        "+async def w(): return []\n"
    )

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=diff)),
        patch.object(pr_reviewer, "boot_dev_server", boot_mock),
    ):
        await pr_reviewer._run_correctness_review(task=task, workspace_root=str(tmp_path))

    boot_mock.assert_called_once()
    _, kwargs = boot_mock.call_args
    assert kwargs.get("repo_id") == 42, f"expected repo_id=42, got {kwargs.get('repo_id')!r}"


@pytest.mark.asyncio
async def test_run_verify_primitives_passes_repo_id_to_boot_dev_server(tmp_path):
    """run_verify_primitives_for_task forwards task.repo_id to boot_dev_server (ADR-019 T3)."""
    from types import SimpleNamespace

    from agent.lifecycle import verify

    task = SimpleNamespace(
        id=2,
        base_branch="main",
        repo_id=99,
    )

    handle = MagicMock()
    handle.state = "disabled"
    handle.teardown = AsyncMock()

    boot_mock = AsyncMock(return_value=handle)

    diff = (
        "diff --git a/api/routes.py b/api/routes.py\n"
        "--- a/api/routes.py\n"
        "+++ b/api/routes.py\n"
        "@@ -1,2 +1,4 @@\n"
        " from fastapi import APIRouter\n"
        "+@router.get('/items')\n"
        "+async def items(): return []\n"
    )

    with (
        patch.object(verify, "_load_diff", AsyncMock(return_value=diff)),
        patch.object(verify, "boot_dev_server", boot_mock),
        patch.object(verify, "_route_primitives_failure", AsyncMock()),
        patch.object(verify, "_write_smoke_result", MagicMock()),
    ):
        await verify.run_verify_primitives_for_task(task=task, workspace_root=str(tmp_path))

    boot_mock.assert_called_once()
    _, kwargs = boot_mock.call_args
    assert kwargs.get("repo_id") == 99, f"expected repo_id=99, got {kwargs.get('repo_id')!r}"


@pytest.mark.asyncio
async def test_scaffold_final_verification_passes_repo_id_to_boot_dev_server(tmp_path):
    """scaffold/final_verification.run forwards task.repo_id to boot_dev_server (ADR-019 T3)."""
    from types import SimpleNamespace

    from agent.lifecycle.scaffold import final_verification as fv_mod

    task = SimpleNamespace(
        id=3,
        repo_id=77,
        status="awaiting_final_verification",
    )

    handle = MagicMock()
    handle.state = "disabled"
    handle.teardown = AsyncMock()

    boot_mock = AsyncMock(return_value=handle)

    with (
        patch.object(fv_mod, "prepare_scaffold_workspace", AsyncMock(return_value=str(tmp_path))),
        patch.object(fv_mod, "_collect_union_routes", AsyncMock(return_value=[])),
        patch.object(fv_mod.verify_primitives, "boot_dev_server", boot_mock),
    ):
        await fv_mod.run(task)

    boot_mock.assert_called_once()
    _, kwargs = boot_mock.call_args
    assert kwargs.get("repo_id") == 77, f"expected repo_id=77, got {kwargs.get('repo_id')!r}"
