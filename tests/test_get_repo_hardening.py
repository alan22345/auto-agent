"""get_repo / get_freeform_config must read the DB in-process, not the HTTP API.

Regression (the real start_coding blocker): ``get_repo`` fetched repos via the
HTTP loopback ``GET /repos``, but that endpoint requires ``current_org_id_dep``
(org-scoped auth). The agent process carries no session cookie or bearer token,
so the call returned **401** and ``handle_coding`` blocked every task with a
misleading "Repo '<name>' not found" — regardless of the repo actually existing.

The fix mirrors ``get_task``: read the repo straight from the DB in-process,
which sidesteps auth entirely. These tests assert get_repo/get_freeform_config
resolve a real repo row without any HTTP call (and that a stray HTTP call would
fail the test).
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

import agent.lifecycle._orchestrator_api as api
from shared.database import async_session
from shared.models import FreeformConfig, Organization, Repo


def _no_http(monkeypatch) -> None:
    """Any HTTP client use is a regression back to the 401-prone loopback."""

    def _boom(*_a, **_k):
        raise AssertionError("get_repo must not make an HTTP call")

    monkeypatch.setattr(api.httpx, "AsyncClient", _boom)


async def _org_id() -> int:
    async with async_session() as s:
        org = (await s.execute(select(Organization).limit(1))).scalar_one_or_none()
        if org is None:
            org = Organization(name="test-org")
            s.add(org)
            await s.commit()
        return org.id


async def _seed_repo(name: str, **cols) -> tuple[int, int]:
    org_id = await _org_id()
    async with async_session() as s:
        existing = (
            await s.execute(select(Repo).where(Repo.name == name))
        ).scalar_one_or_none()
        if existing is not None:
            await s.execute(delete(FreeformConfig).where(FreeformConfig.repo_id == existing.id))
            await s.delete(existing)
            await s.commit()
        repo = Repo(name=name, url=f"https://github.com/x/{name}", organization_id=org_id, **cols)
        s.add(repo)
        await s.commit()
        return repo.id, org_id


@pytest.mark.asyncio
async def test_get_repo_reads_db_without_http(monkeypatch):
    _no_http(monkeypatch)
    repo_id, _ = await _seed_repo("causal-tool-data-generator")
    repo = await api.get_repo("causal-tool-data-generator")
    assert repo is not None
    assert repo.id == repo_id
    assert repo.name == "causal-tool-data-generator"


@pytest.mark.asyncio
async def test_get_repo_missing_returns_none(monkeypatch):
    _no_http(monkeypatch)
    assert await api.get_repo("nope-does-not-exist") is None


@pytest.mark.asyncio
async def test_get_freeform_config_reads_db_without_http(monkeypatch):
    _no_http(monkeypatch)
    repo_id, org_id = await _seed_repo("ff-repo")
    async with async_session() as s:
        s.add(FreeformConfig(
            repo_id=repo_id, organization_id=org_id,
            enabled=True, dev_branch="dev", prod_branch="main",
        ))
        await s.commit()
    cfg = await api.get_freeform_config("ff-repo")
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.dev_branch == "dev"


@pytest.mark.asyncio
async def test_get_freeform_config_disabled_returns_none(monkeypatch):
    _no_http(monkeypatch)
    repo_id, org_id = await _seed_repo("ff-disabled")
    async with async_session() as s:
        s.add(FreeformConfig(
            repo_id=repo_id, organization_id=org_id, enabled=False, dev_branch="dev",
        ))
        await s.commit()
    assert await api.get_freeform_config("ff-disabled") is None


@pytest.mark.asyncio
async def test_mark_repo_harness_onboarded_writes_db(monkeypatch):
    """Onboarding marks the repo in-process; the old POST /repos/{id}/harness
    loopback 401'd, so onboarding re-fired forever opening duplicate PRs."""
    _no_http(monkeypatch)
    repo_id, _ = await _seed_repo("onboard-repo", harness_onboarded=False)
    await api.mark_repo_harness_onboarded(repo_id, "https://github.com/x/onboard-repo/pull/1")
    async with async_session() as s:
        repo = (await s.execute(select(Repo).where(Repo.id == repo_id))).scalar_one()
        assert repo.harness_onboarded is True
        assert repo.harness_pr_url.endswith("/pull/1")
