"""End-to-end pairing flow with a fake `claude` binary in a PTY."""
import asyncio
import os
import stat
from pathlib import Path

from orchestrator import claude_pairing as cp

FAKE_CLAUDE = """\
#!/usr/bin/env bash
set -e
echo "Open this URL in your browser:"
echo "https://claude.ai/login?code=fake-pairing-token-abc"
echo "Paste the code here:"
read code
mkdir -p "$HOME/.claude"
echo '{"token":"got-'"$code"'"}' > "$HOME/.claude/.credentials.json"
echo "Login successful."
"""


def _install_fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


async def test_pairing_full_round_trip(tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "orchestrator.claude_auth.settings.users_data_dir",
        str(tmp_path / "vaults"),
    )

    session = await cp.start_pairing(user_id=42)

    url_seen = None
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        line = await session.read_line(timeout=0.5)
        if line and "https://claude.ai" in line:
            url_seen = line.strip()
            break
    assert url_seen is not None, "fake claude never emitted a URL"

    await session.submit_code("CODE-123")

    result = await session.wait_for_exit(timeout=5.0)
    assert result.success is True, f"stderr={result.stderr}"

    cred = Path(tmp_path) / "vaults" / "42" / ".claude" / ".credentials.json"
    assert cred.exists()
    assert "got-CODE-123" in cred.read_text()


async def test_pairing_session_registry_ttl(tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "orchestrator.claude_auth.settings.users_data_dir",
        str(tmp_path / "vaults"),
    )
    session = await cp.start_pairing(user_id=1)
    looked_up = cp.get_pairing(session.pairing_id)
    assert looked_up is session
    await session.cancel()
    assert cp.get_pairing(session.pairing_id) is None
