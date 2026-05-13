"""Regression tests for orchestrator.router._get_repo_in_org.

Old behaviour OR-ed ``name == X`` with ``name endswith "/X"``, so when a
legacy ``owner/X`` repo lived alongside a newer bare ``X`` repo in the
same org the query returned both rows and ``scalar_one_or_none`` raised
``MultipleResultsFound`` — taking down POST /api/tasks for any caller
who passed the short repo name.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import _get_repo_in_org
from shared.models import Repo


def _mock_repo(name: str, repo_id: int = 1) -> Repo:
    r = MagicMock(spec=Repo)
    r.id = repo_id
    r.name = name
    return r


def _exec_result(*, scalar_one_or_none=None, first=None):
    """Build the chained result mock for either entry point."""
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    scalars = MagicMock()
    scalars.first = MagicMock(return_value=first)
    res.scalars = MagicMock(return_value=scalars)
    return res


@pytest.mark.asyncio
async def test_returns_exact_match_when_both_exact_and_suffix_exist():
    """The original bug: exact match wins so MultipleResultsFound never fires."""
    exact = _mock_repo("cardamon", repo_id=10)
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_exec_result(scalar_one_or_none=exact))

    out = await _get_repo_in_org(session, name="cardamon", org_id=1)

    assert out is exact
    # Only the exact-match query should run when it hits.
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_falls_back_to_suffix_when_no_exact_match():
    """Legacy callers passing 'cardamon' for an org with only 'owner/cardamon'."""
    legacy = _mock_repo("zubmag/cardamon", repo_id=10)
    session = AsyncMock(spec=AsyncSession)
    # First execute (exact) returns None; second (suffix) returns the legacy row.
    session.execute = AsyncMock(side_effect=[
        _exec_result(scalar_one_or_none=None),
        _exec_result(first=legacy),
    ])

    out = await _get_repo_in_org(session, name="cardamon", org_id=1)

    assert out is legacy
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_no_suffix_lookup_when_name_already_qualified():
    """A name containing '/' is already an owner/repo, don't suffix-match further."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_exec_result(scalar_one_or_none=None))

    out = await _get_repo_in_org(session, name="zubmag/cardamon", org_id=1)

    assert out is None
    # Exact only; the suffix branch is skipped because '/' is already in the name.
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_returns_none_when_nothing_matches():
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[
        _exec_result(scalar_one_or_none=None),
        _exec_result(first=None),
    ])
    out = await _get_repo_in_org(session, name="does-not-exist", org_id=1)
    assert out is None


@pytest.mark.asyncio
async def test_repo_id_path_returns_scalar_one_or_none():
    repo = _mock_repo("anything", repo_id=42)
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_exec_result(scalar_one_or_none=repo))

    out = await _get_repo_in_org(session, repo_id=42, org_id=1)

    assert out is repo
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_raises_without_repo_id_or_name():
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError, match="repo_id or name is required"):
        await _get_repo_in_org(session, org_id=1)
