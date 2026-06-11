"""get_repo must survive a malformed repo in the /repos payload.

Regression: ``get_repo`` validated EVERY repo dict with
``RepoData.model_validate`` while scanning for the one it wanted. A single
malformed repo (e.g. one whose graph/serialization was broken) threw out of
the loop and aborted ``handle_coding`` before it logged anything — every
coding task silently failed on ``start_coding``. The fix matches on the raw
name first and validates only the target repo, handling failure gracefully.
"""
from __future__ import annotations

import pytest

import agent.lifecycle._orchestrator_api as api


class _FakeResp:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, _url):
        return self._resp


def _patch_httpx(monkeypatch, resp: _FakeResp) -> None:
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))


_VALID_TARGET = {"id": 2, "name": "good-repo", "url": "https://x/good", "author": "me"}
# Missing required url + author — would raise ValidationError if validated.
_MALFORMED = {"id": 1, "name": "bad-other-repo"}


@pytest.mark.asyncio
async def test_get_repo_skips_malformed_other_repo(monkeypatch):
    """A malformed *other* repo earlier in the list must not break the lookup."""
    _patch_httpx(monkeypatch, _FakeResp(200, [_MALFORMED, _VALID_TARGET]))
    repo = await api.get_repo("good-repo")
    assert repo is not None
    assert repo.name == "good-repo"


@pytest.mark.asyncio
async def test_get_repo_non_200_returns_none(monkeypatch):
    """A non-200 from /repos returns None instead of throwing on resp.json()."""
    _patch_httpx(monkeypatch, _FakeResp(500, None))
    assert await api.get_repo("good-repo") is None


@pytest.mark.asyncio
async def test_get_repo_target_invalid_returns_none(monkeypatch):
    """If the TARGET repo itself fails validation, return None (logged) — never
    raise, so the caller can transition to a clean 'repo not found' block."""
    bad_target = {"id": 1, "name": "good-repo"}  # missing url + author
    _patch_httpx(monkeypatch, _FakeResp(200, [bad_target]))
    assert await api.get_repo("good-repo") is None
