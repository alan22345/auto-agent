"""PATCH /api/repos/{id}/product-brief sets Repo.product_brief."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import ProductBriefIn, update_repo_product_brief
from shared.models import Repo


@pytest.mark.asyncio
async def test_patch_writes_product_brief():
    session = AsyncMock(spec=AsyncSession)
    repo = MagicMock(spec=Repo)
    repo.id = 5
    repo.name = "test"
    repo.url = "https://github.com/test/test.git"
    repo.default_branch = "main"
    repo.summary = None
    repo.summary_updated_at = None
    repo.ci_checks = None
    repo.harness_onboarded = False
    repo.harness_pr_url = None
    repo.product_brief = None
    with patch("orchestrator.router._get_repo_in_org",
               AsyncMock(return_value=repo)):
        out = await update_repo_product_brief(
            repo_id=5,
            body=ProductBriefIn(product_brief="# Mission"),
            session=session,
            org_id=1,
        )
    assert repo.product_brief == "# Mission"
    assert out.product_brief == "# Mission"


@pytest.mark.asyncio
async def test_patch_404_when_repo_missing():
    session = AsyncMock(spec=AsyncSession)
    with (
        patch(
            "orchestrator.router._get_repo_in_org",
            AsyncMock(return_value=None),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await update_repo_product_brief(
            repo_id=999,
            body=ProductBriefIn(product_brief="x"),
            session=session, org_id=1,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_patch_accepts_empty_string_to_clear():
    session = AsyncMock(spec=AsyncSession)
    repo = MagicMock(spec=Repo)
    repo.id = 5
    repo.name = "test"
    repo.url = "https://github.com/test/test.git"
    repo.default_branch = "main"
    repo.summary = None
    repo.summary_updated_at = None
    repo.ci_checks = None
    repo.harness_onboarded = False
    repo.harness_pr_url = None
    repo.product_brief = "old"
    with patch("orchestrator.router._get_repo_in_org",
               AsyncMock(return_value=repo)):
        out = await update_repo_product_brief(
            repo_id=5,
            body=ProductBriefIn(product_brief=""),
            session=session, org_id=1,
        )
    # Empty string clears it (we treat empty as null on write).
    assert repo.product_brief is None
    assert out.product_brief is None
