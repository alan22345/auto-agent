"""Tests for GET /api/tasks/{id}/decisions."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import (
    _adr_title_from_markdown,
    _decisions_ref_for_task,
    list_decisions,
)
from shared.models import Repo, Task, TrioPhase


def _mock_task(
    task_id: int = 1,
    *,
    parent_task_id: int | None = None,
    trio_phase: TrioPhase | None = None,
    branch_name: str | None = None,
    repo: Repo | None = None,
):
    t = MagicMock(spec=Task)
    t.id = task_id
    t.parent_task_id = parent_task_id
    t.trio_phase = trio_phase
    t.branch_name = branch_name
    t.repo = repo
    t.repo_id = repo.id if repo else None
    t.created_by_user_id = 1
    t.organization_id = 1
    return t


def _mock_repo(
    url: str = "https://github.com/acme/widgets.git",
    default_branch: str = "main",
    repo_id: int = 5,
):
    r = MagicMock(spec=Repo)
    r.id = repo_id
    r.url = url
    r.default_branch = default_branch
    return r


def test_adr_title_from_markdown_picks_first_h1():
    assert _adr_title_from_markdown(
        "# Foo\n\n## Bar\n", "fallback",
    ) == "Foo"
    assert _adr_title_from_markdown(
        "no heading\n", "fallback",
    ) == "fallback"
    assert _adr_title_from_markdown(
        "## Subheading first\n# Real Title\n", "fallback",
    ) == "Real Title"


def test_decisions_ref_for_task_picks_trio_branches():
    repo = _mock_repo()
    # trio child
    child = _mock_task(task_id=42, parent_task_id=7, repo=repo)
    assert _decisions_ref_for_task(child) == "trio/7"
    # trio parent (parent_task_id None, trio_phase set)
    parent = _mock_task(task_id=7, trio_phase=TrioPhase.ARCHITECTING, repo=repo)
    assert _decisions_ref_for_task(parent) == "trio/7"
    # non-trio with branch_name
    branched = _mock_task(task_id=9, branch_name="feature/foo", repo=repo)
    assert _decisions_ref_for_task(branched) == "feature/foo"
    # non-trio without branch_name → repo default
    plain = _mock_task(task_id=9, repo=repo)
    assert _decisions_ref_for_task(plain) == "main"
    # no repo → None
    no_repo = _mock_task(task_id=9, repo=None)
    no_repo.repo = None
    assert _decisions_ref_for_task(no_repo) is None


@pytest.mark.asyncio
async def test_list_decisions_404_when_task_missing():
    session = AsyncMock(spec=AsyncSession)
    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await list_decisions(task_id=999, session=session, org_id=1)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_decisions_empty_when_no_repo():
    session = AsyncMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=None)
    task = _mock_task(task_id=1, repo=None)
    task.repo_id = None
    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=task),
    ):
        out = await list_decisions(task_id=1, session=session, org_id=1)
    assert out == []


@pytest.mark.asyncio
async def test_list_decisions_empty_when_url_not_github():
    session = AsyncMock(spec=AsyncSession)
    repo = _mock_repo(url="https://gitlab.com/acme/widgets.git")
    session.get = AsyncMock(return_value=repo)
    task = _mock_task(task_id=1, repo=repo)
    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=task),
    ):
        out = await list_decisions(task_id=1, session=session, org_id=1)
    assert out == []


@pytest.mark.asyncio
async def test_list_decisions_empty_when_no_token():
    session = AsyncMock(spec=AsyncSession)
    repo = _mock_repo()
    session.get = AsyncMock(return_value=repo)
    task = _mock_task(task_id=1, repo=repo)
    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=task),
    ), patch(
        "shared.github_auth.get_github_token", AsyncMock(return_value=None),
    ):
        out = await list_decisions(task_id=1, session=session, org_id=1)
    assert out == []


@pytest.mark.asyncio
async def test_list_decisions_returns_files_with_h1_titles():
    """Happy path: listing returns 2 md files; per-file raw fetch returns content."""
    session = AsyncMock(spec=AsyncSession)
    repo = _mock_repo()
    session.get = AsyncMock(return_value=repo)
    task = _mock_task(task_id=1, parent_task_id=7, repo=repo)

    listing_payload = [
        {
            "type": "file",
            "name": "001-bedrock-default.md",
            "url": "https://api.github.com/repos/acme/widgets/contents/docs/decisions/001-bedrock-default.md",
            "html_url": "https://github.com/acme/widgets/blob/trio/7/docs/decisions/001-bedrock-default.md",
        },
        {
            "type": "file",
            "name": "000-template.md",  # skipped
            "url": "https://api.github.com/repos/acme/widgets/contents/docs/decisions/000-template.md",
            "html_url": "https://github.com/acme/widgets/blob/trio/7/docs/decisions/000-template.md",
        },
        {
            "type": "file",
            "name": "002-llm-routing.md",
            "url": "https://api.github.com/repos/acme/widgets/contents/docs/decisions/002-llm-routing.md",
            "html_url": "https://github.com/acme/widgets/blob/trio/7/docs/decisions/002-llm-routing.md",
        },
        {
            "type": "dir",  # skipped
            "name": "subdir",
        },
        {
            "type": "file",
            "name": "README.txt",  # skipped (not .md)
            "url": "https://api.github.com/...",
            "html_url": "https://github.com/...",
        },
    ]
    raw_by_url = {
        listing_payload[0]["url"]: "# Use Bedrock as default LLM provider\n\nBody",
        listing_payload[2]["url"]: "no heading here, fallback used",
    }

    def _route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents/docs/decisions"):
            return httpx.Response(200, json=listing_payload)
        return httpx.Response(200, text=raw_by_url.get(str(request.url).split("?")[0], ""))

    transport = httpx.MockTransport(_route)
    real_async_client = httpx.AsyncClient

    def _client_factory(*a, **kw):
        return real_async_client(*a, transport=transport, **kw)

    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=task),
    ), patch(
        "shared.github_auth.get_github_token", AsyncMock(return_value="ghp_test"),
    ), patch(
        "httpx.AsyncClient",
        _client_factory,
    ):
        out = await list_decisions(task_id=1, session=session, org_id=1)

    # 000-template skipped; dir skipped; non-md skipped.
    assert [d.filename for d in out] == ["001-bedrock-default.md", "002-llm-routing.md"]
    assert out[0].title == "Use Bedrock as default LLM provider"
    # No heading → fallback derived from filename.
    assert out[1].title == "llm routing"
    assert out[0].url.endswith("001-bedrock-default.md")


@pytest.mark.asyncio
async def test_list_decisions_empty_when_directory_404s():
    session = AsyncMock(spec=AsyncSession)
    repo = _mock_repo()
    session.get = AsyncMock(return_value=repo)
    task = _mock_task(task_id=1, repo=repo)

    transport = httpx.MockTransport(lambda req: httpx.Response(404, json={"message": "Not Found"}))
    real_async_client = httpx.AsyncClient

    def _client_factory(*a, **kw):
        return real_async_client(*a, transport=transport, **kw)

    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=task),
    ), patch(
        "shared.github_auth.get_github_token", AsyncMock(return_value="ghp_test"),
    ), patch(
        "httpx.AsyncClient",
        _client_factory,
    ):
        out = await list_decisions(task_id=1, session=session, org_id=1)
    assert out == []
