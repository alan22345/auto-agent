"""Auth probe classifies stderr signatures into paired/expired."""
from unittest.mock import AsyncMock, patch

from orchestrator import claude_auth


async def test_probe_paired_on_clean_exit(tmp_path):
    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hi", b""))
    fake_proc.returncode = 0
    with patch(
        "orchestrator.claude_auth.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        status = await claude_auth.probe_credentials(str(tmp_path))
    assert status == "paired"


async def test_probe_expired_on_unauthorized_stderr(tmp_path):
    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(
        return_value=(b"", b"Error: unauthorized -- please log in again.")
    )
    fake_proc.returncode = 1
    with patch(
        "orchestrator.claude_auth.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        status = await claude_auth.probe_credentials(str(tmp_path))
    assert status == "expired"


async def test_probe_expired_on_missing_binary(tmp_path):
    with patch(
        "orchestrator.claude_auth.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=FileNotFoundError),
    ):
        status = await claude_auth.probe_credentials(str(tmp_path))
    assert status == "expired"
