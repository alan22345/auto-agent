"""Tests for the per-user lookup in shared.github_auth.get_github_token."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared import github_auth


@pytest.fixture(autouse=True)
def _reset_cache():
    github_auth.reset_cache()
    yield
    github_auth.reset_cache()


async def test_user_pat_wins_over_app_and_env():
    """When user_id is supplied and that user has a PAT, use it — even if
    the App is configured and an env PAT is set."""
    with (
        patch("shared.secrets.get", new=AsyncMock(return_value="ghu_user_pat")),
        patch.object(github_auth.settings, "github_app_id", "12345"),
        patch.object(github_auth.settings, "github_app_private_key", "key"),
        patch.object(github_auth.settings, "github_app_installation_id", "67890"),
        patch.object(github_auth.settings, "github_token", "ghp_env"),
    ):
        token = await github_auth.get_github_token(user_id=42, organization_id=1)
    assert token == "ghu_user_pat"


async def test_user_with_no_pat_falls_through_to_env_when_no_app():
    with (
        patch("shared.secrets.get", new=AsyncMock(return_value=None)),
        patch.object(github_auth.settings, "github_app_id", ""),
        patch.object(github_auth.settings, "github_token", "ghp_env"),
    ):
        token = await github_auth.get_github_token(user_id=42, organization_id=1)
    assert token == "ghp_env"


async def test_no_user_id_falls_through_to_env_when_no_app():
    """The original (no-arg) behaviour is preserved."""
    with (
        patch.object(github_auth.settings, "github_app_id", ""),
        patch.object(github_auth.settings, "github_token", "ghp_env"),
    ):
        token = await github_auth.get_github_token()
    assert token == "ghp_env"


async def test_user_pat_lookup_failure_falls_through_silently():
    """If the secrets store blows up, we don't 500 — we fall through."""
    with (
        patch(
            "shared.secrets.get",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch.object(github_auth.settings, "github_app_id", ""),
        patch.object(github_auth.settings, "github_token", "ghp_env"),
    ):
        token = await github_auth.get_github_token(user_id=42, organization_id=1)
    assert token == "ghp_env"


async def test_empty_user_pat_falls_through_to_app_or_env():
    """Empty-string PAT (legacy stored row) is treated as 'no PAT'."""
    with (
        patch("shared.secrets.get", new=AsyncMock(return_value="")),
        patch.object(github_auth.settings, "github_app_id", ""),
        patch.object(github_auth.settings, "github_token", "ghp_env"),
    ):
        token = await github_auth.get_github_token(user_id=42, organization_id=1)
    assert token == "ghp_env"
