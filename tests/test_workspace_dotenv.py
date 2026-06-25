"""Tests for write_repo_dotenv — ADR-019 T4.

Covers:
- Happy-path: .env is created from RepoSecret rows.
- Overwrite: existing .env is wholesale-replaced (stale keys disappear).
- .gitignore creation when absent.
- .gitignore append when present but .env is missing.
- .gitignore idempotency (no duplicate .env line).
- Dotenv escaping for values with newlines / double-quotes / backslashes.
- File permissions are 0o600.
- Graceful repo-not-found (logs warning, no .env created).
- Empty secrets → empty .env file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helper: build the standard mock chain that write_repo_dotenv uses.
# ---------------------------------------------------------------------------


def _make_repo_mock(org_id: int = 42) -> MagicMock:
    repo = MagicMock()
    repo.organization_id = org_id
    return repo


def _patch_db(repo_mock, secrets: dict[str, str]):
    """Return a context-manager stack that patches the DB session + secrets.

    ``repo_mock`` is what ``session.execute(...).scalar_one_or_none()`` returns.
    ``secrets`` is what ``repo_secrets.get_all_for_boot`` returns.

    Patches at the *source* module paths because write_repo_dotenv uses
    local (deferred) imports inside the function body.
    """
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=repo_mock))
    )
    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    return (
        patch("shared.database.async_session", return_value=mock_session_cm),
        patch(
            "shared.repo_secrets.get_all_for_boot",
            new=AsyncMock(return_value=secrets),
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_repo_dotenv_creates_env_file(tmp_path: Path):
    """Happy path: .env is written with the correct KEY=VALUE lines."""
    secrets = {"STRIPE_API_KEY": "sk_test_123", "POSTGRES_URL": "postgres://localhost/db"}
    p1, p2 = _patch_db(_make_repo_mock(), secrets)
    with p1, p2:
        from agent.workspace import _write_repo_dotenv

        await _write_repo_dotenv(tmp_path, repo_id=1)

    env_path = tmp_path / ".env"
    assert env_path.exists(), ".env was not created"
    content = env_path.read_text()
    assert "STRIPE_API_KEY=sk_test_123" in content
    assert "POSTGRES_URL=postgres://localhost/db" in content


@pytest.mark.asyncio
async def test_write_repo_dotenv_overwrites_existing(tmp_path: Path):
    """Pre-existing .env is wholesale-replaced; stale keys disappear."""
    (tmp_path / ".env").write_text("OLD_KEY=should_be_gone\n")
    secrets = {"NEW_KEY": "keep"}
    p1, p2 = _patch_db(_make_repo_mock(), secrets)
    with p1, p2:
        from agent.workspace import _write_repo_dotenv

        await _write_repo_dotenv(tmp_path, repo_id=1)

    content = (tmp_path / ".env").read_text()
    assert "OLD_KEY" not in content
    assert "NEW_KEY=keep" in content


@pytest.mark.asyncio
async def test_write_repo_dotenv_creates_gitignore_if_absent(tmp_path: Path):
    """.gitignore is created with .env when it doesn't exist."""
    secrets = {"FOO": "bar"}
    p1, p2 = _patch_db(_make_repo_mock(), secrets)
    with p1, p2:
        from agent.workspace import _write_repo_dotenv

        await _write_repo_dotenv(tmp_path, repo_id=1)

    gi = tmp_path / ".gitignore"
    assert gi.exists(), ".gitignore was not created"
    lines = [line.strip() for line in gi.read_text().splitlines()]
    assert ".env" in lines


@pytest.mark.asyncio
async def test_write_repo_dotenv_appends_env_to_existing_gitignore(tmp_path: Path):
    """When .gitignore exists but doesn't contain .env, the line is appended."""
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    secrets = {"FOO": "bar"}
    p1, p2 = _patch_db(_make_repo_mock(), secrets)
    with p1, p2:
        from agent.workspace import _write_repo_dotenv

        await _write_repo_dotenv(tmp_path, repo_id=1)

    content = (tmp_path / ".gitignore").read_text()
    assert "node_modules/" in content
    assert ".env" in content


@pytest.mark.asyncio
async def test_write_repo_dotenv_does_not_duplicate_env_in_gitignore(tmp_path: Path):
    """When .gitignore already has .env, no duplicate line is added."""
    (tmp_path / ".gitignore").write_text(".env\nfoo\n")
    secrets = {"FOO": "bar"}
    p1, p2 = _patch_db(_make_repo_mock(), secrets)
    with p1, p2:
        from agent.workspace import _write_repo_dotenv

        await _write_repo_dotenv(tmp_path, repo_id=1)

    content = (tmp_path / ".gitignore").read_text()
    lines = [line.strip() for line in content.splitlines()]
    assert lines.count(".env") == 1, f"Expected exactly one .env line, got: {lines}"


@pytest.mark.asyncio
async def test_write_repo_dotenv_escapes_special_values(tmp_path: Path):
    """Values with newlines or double-quotes are wrapped in double quotes."""
    secrets = {
        "WITH_NEWLINE": "line1\nline2",
        "WITH_QUOTES": 'val "with" quotes',
        "WITH_BACKSLASH": "path\\to\\thing",
    }
    p1, p2 = _patch_db(_make_repo_mock(), secrets)
    with p1, p2:
        from agent.workspace import _write_repo_dotenv

        await _write_repo_dotenv(tmp_path, repo_id=1)

    content = (tmp_path / ".env").read_text()

    # Values with special chars must be double-quoted in the output.
    # The newline can appear either escaped as \n or as a literal newline inside the quoted value.
    assert '"line1\\nline2"' in content or '"line1\nline2"' in content
    assert 'WITH_QUOTES="' in content
    assert 'WITH_BACKSLASH="' in content

    # Also verify round-trip via python-dotenv if available.
    try:
        import dotenv
        parsed = dotenv.dotenv_values(tmp_path / ".env")
        assert parsed["WITH_NEWLINE"] == "line1\nline2"
        assert parsed["WITH_QUOTES"] == 'val "with" quotes'
        assert parsed["WITH_BACKSLASH"] == "path\\to\\thing"
    except ImportError:
        pass  # python-dotenv not installed; skip round-trip check


@pytest.mark.asyncio
async def test_write_repo_dotenv_file_permissions(tmp_path: Path):
    """The .env file is written with 0o600 permissions (owner read/write only)."""
    secrets = {"SECRET": "value"}
    p1, p2 = _patch_db(_make_repo_mock(), secrets)
    with p1, p2:
        from agent.workspace import _write_repo_dotenv

        await _write_repo_dotenv(tmp_path, repo_id=1)

    mode = (tmp_path / ".env").stat().st_mode & 0o777
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


@pytest.mark.asyncio
async def test_write_repo_dotenv_no_repo_warns_and_returns(tmp_path: Path, caplog):
    """When the Repo row doesn't exist, log a warning and don't create .env."""
    # repo_mock = None → scalar_one_or_none() returns None
    p1, p2 = _patch_db(None, {})
    with p1, p2:
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs:
            from agent.workspace import _write_repo_dotenv

            await _write_repo_dotenv(tmp_path, repo_id=999)

    assert not (tmp_path / ".env").exists(), ".env should NOT be created when repo is not found"
    event_names = [entry.get("event") for entry in cap_logs]
    assert "write_repo_dotenv.repo_not_found" in event_names


@pytest.mark.asyncio
async def test_write_repo_dotenv_empty_secrets_writes_empty_file(tmp_path: Path):
    """When there are no secrets, .env is created and is empty (or just whitespace)."""
    p1, p2 = _patch_db(_make_repo_mock(), {})
    with p1, p2:
        from agent.workspace import _write_repo_dotenv

        await _write_repo_dotenv(tmp_path, repo_id=1)

    env_path = tmp_path / ".env"
    assert env_path.exists(), ".env should be created even when secrets dict is empty"
    assert env_path.read_text().strip() == "", "Empty secrets should produce an empty .env"


@pytest.mark.asyncio
async def test_write_repo_dotenv_preserves_literal_dollar_brace(tmp_path: Path):
    """Values containing ${...} must round-trip literally, not be interpolated.

    python-dotenv resolves ${VAR} references when interpolate=True (default).
    Callers that need literal ${...} in secrets must use interpolate=False.
    This test verifies the file encoding is correct (values survive unmodified)
    by reading back with interpolate=False.
    """
    secrets = {
        "MULTILINE_WITH_TEMPLATE": "line1\n${DB_HOST} not interpolated",
        "JUST_TEMPLATE": "${SHOULD_STAY_LITERAL}",
    }
    p1, p2 = _patch_db(_make_repo_mock(), secrets)
    with p1, p2:
        from agent.workspace import _write_repo_dotenv

        await _write_repo_dotenv(tmp_path, repo_id=1)

    try:
        import dotenv
    except ImportError:
        pytest.skip("python-dotenv not installed; can't verify roundtrip")

    # Use interpolate=False so ${...} references are not expanded — we are
    # verifying the *encoding* of the file, not variable expansion behaviour.
    parsed = dotenv.dotenv_values(tmp_path / ".env", interpolate=False)
    assert parsed["MULTILINE_WITH_TEMPLATE"] == "line1\n${DB_HOST} not interpolated"
    assert parsed["JUST_TEMPLATE"] == "${SHOULD_STAY_LITERAL}"
