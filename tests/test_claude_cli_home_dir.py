"""ClaudeCLIProvider must spawn the subprocess with HOME=<vault_dir>."""
from unittest.mock import AsyncMock, patch

from agent.llm.claude_cli import ClaudeCLIProvider


async def test_invoke_passes_home_env(tmp_path):
    provider = ClaudeCLIProvider()
    provider.set_home_dir(str(tmp_path))

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hello", b""))
    fake_proc.returncode = 0

    with patch(
        "agent.llm.claude_cli.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as spawn:
        await provider._invoke_cli_once("ping")

    kwargs = spawn.call_args.kwargs
    assert "env" in kwargs, "subprocess must receive env"
    assert kwargs["env"]["HOME"] == str(tmp_path)


async def test_invoke_without_home_dir_inherits_env():
    provider = ClaudeCLIProvider()

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hi", b""))
    fake_proc.returncode = 0

    with patch(
        "agent.llm.claude_cli.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as spawn:
        await provider._invoke_cli_once("ping")

    # When home_dir is unset, we don't override; subprocess inherits parent env.
    kwargs = spawn.call_args.kwargs
    assert "env" not in kwargs or kwargs["env"] is None
