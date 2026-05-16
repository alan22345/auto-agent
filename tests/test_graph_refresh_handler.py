"""Graph-refresh event handler tests (ADR-016 Phase 2).

The handler is the bridge between the orchestrator's HTTP layer and the
analyser. It must:

* clone the workspace on first run, fetch+reset on subsequent runs;
* call the pipeline with the resolved ``commit_sha``;
* write a ``RepoGraph`` row + update the config;
* publish ``REPO_GRAPH_READY`` on success;
* publish ``REPO_GRAPH_FAILED`` on lock contention, clone failure, or
  DB write failure — never raise.

These tests stub the git subprocess and the SQLAlchemy session boundaries
so they run without Postgres or a real ``git`` binary.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.events import InMemoryPublisher, RepoEventType
from shared.types import AreaStatus, RepoGraphBlob

_NOW = "2026-05-15T00:00:00+00:00"


def _make_blob(*, status_per_area=("ok",), commit_sha: str = "abc"):
    from datetime import UTC, datetime

    return RepoGraphBlob(
        commit_sha=commit_sha,
        generated_at=datetime.now(UTC),
        analyser_version="phase2-python-test",
        areas=[AreaStatus(name=f"a{i}", status=s) for i, s in enumerate(status_per_area)],
        nodes=[],
        edges=[],
    )


class _SessionStub:
    """Minimal async-session stub. Captures ``add``/``commit``/``flush``/
    ``refresh`` calls and serves ``session.execute(select(...))`` via the
    fixtures wired in below.
    """

    def __init__(self, *, cfg, repo, added: list):
        self._cfg = cfg
        self._repo = repo
        self._added = added
        self._row_id_counter = 100

    async def execute(self, stmt):
        # Hand back either the cfg or the repo depending on what's
        # being selected. We disambiguate via the statement's __str__
        # (cheap and stable for these tests).
        s = str(stmt).lower()
        result = MagicMock()
        if "repo_graph_configs" in s:
            result.scalar_one_or_none.return_value = self._cfg
        elif "repos" in s:
            result.scalar_one_or_none.return_value = self._repo
        else:
            result.scalar_one_or_none.return_value = None
        return result

    def add(self, obj):
        self._row_id_counter += 1
        obj.id = self._row_id_counter
        self._added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, obj):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


@pytest.fixture(autouse=True)
def _graph_workspaces_env(tmp_path: Path, monkeypatch):
    """Pin ``GRAPH_WORKSPACES_DIR`` per-test — the lock's root-dir
    create needs a writeable location."""
    monkeypatch.setenv("GRAPH_WORKSPACES_DIR", str(tmp_path / "graph-workspaces"))


@pytest.fixture
def fake_session():
    """Yields a freshly-built ``_SessionStub`` factory + the list of
    objects added to it across the test (so assertions can see the new
    ``RepoGraph`` row)."""
    cfg = MagicMock()
    cfg.repo_id = 7
    cfg.organization_id = 1
    cfg.analysis_branch = "main"
    cfg.workspace_path = "/data/graph-workspaces/7"
    cfg.last_analysis_id = None
    cfg.analyser_version = ""

    repo = MagicMock()
    repo.id = 7
    repo.url = "https://github.com/example/demo.git"
    repo.organization_id = 1

    added: list = []

    def factory():
        return _SessionStub(cfg=cfg, repo=repo, added=added)

    return cfg, repo, added, factory


# ----------------------------------------------------------------------
# Happy path — clone, analyse, write row, publish READY
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_clones_and_publishes_ready(
    fake_session,
    publisher: InMemoryPublisher,
    tmp_path: Path,
) -> None:
    cfg, repo, added, factory = fake_session
    cfg.workspace_path = str(tmp_path / "ws-7")  # doesn't exist yet → clone path

    git_calls: list[list[str]] = []

    async def fake_run_git(*, args, cwd):
        git_calls.append(args)
        # ``rev-parse`` returns the sha; everything else just succeeds.
        if args[:1] == ["rev-parse"]:
            return "deadbee1234\n"
        # Make the workspace .git dir exist so subsequent code believes
        # the clone succeeded.
        if args[:1] == ["clone"]:
            workspace = args[-1]
            (Path(workspace) / ".git").mkdir(parents=True, exist_ok=True)
        return ""

    blob = _make_blob(status_per_area=("ok", "ok"), commit_sha="deadbee1234")

    with (
        patch("agent.lifecycle.graph_refresh.async_session", new=factory),
        patch("agent.lifecycle.graph_refresh._run_git", new=fake_run_git),
        patch("agent.lifecycle.graph_refresh.run_pipeline", new=AsyncMock(return_value=blob)),
    ):
        from agent.lifecycle.graph_refresh import run_refresh

        await run_refresh(repo_id=7, request_id="r-1")

    # First-run clone happened.
    assert git_calls[0][:1] == ["clone"]
    assert "--depth=1" in git_calls[0]
    assert git_calls[0][-2] == repo.url
    # ``rev-parse HEAD`` resolved the commit sha.
    assert any(c[:1] == ["rev-parse"] for c in git_calls)

    # A RepoGraph row was added, status=ok.
    assert len(added) == 1
    row = added[0]
    assert row.repo_id == 7
    assert row.commit_sha == "deadbee1234"
    assert row.status == "ok"
    # ``graph_json`` is a dict (JSONB-friendly).
    assert isinstance(row.graph_json, dict)
    assert row.graph_json["commit_sha"] == "deadbee1234"

    # Config last_analysis_id + analyser_version updated.
    assert cfg.last_analysis_id == row.id
    assert cfg.analyser_version == blob.analyser_version

    # READY published with the right payload.
    ready_events = [e for e in publisher.events if e.type == RepoEventType.GRAPH_READY]
    assert len(ready_events) == 1
    p = ready_events[0].payload
    assert p["repo_id"] == 7
    assert p["repo_graph_id"] == row.id
    assert p["commit_sha"] == "deadbee1234"
    assert p["status"] == "ok"


@pytest.mark.asyncio
async def test_subsequent_run_fetches_and_resets(
    fake_session,
    publisher: InMemoryPublisher,
    tmp_path: Path,
) -> None:
    cfg, _repo, _added, factory = fake_session
    workspace = tmp_path / "ws-7"
    (workspace / ".git").mkdir(parents=True)
    cfg.workspace_path = str(workspace)

    git_calls: list[list[str]] = []

    async def fake_run_git(*, args, cwd):
        git_calls.append(args)
        if args[:1] == ["rev-parse"]:
            return "freshbee\n"
        return ""

    blob = _make_blob(status_per_area=("ok",), commit_sha="freshbee")

    with (
        patch("agent.lifecycle.graph_refresh.async_session", new=factory),
        patch("agent.lifecycle.graph_refresh._run_git", new=fake_run_git),
        patch("agent.lifecycle.graph_refresh.run_pipeline", new=AsyncMock(return_value=blob)),
    ):
        from agent.lifecycle.graph_refresh import run_refresh

        await run_refresh(repo_id=7, request_id="r-2")

    # No clone happened; fetch + reset --hard origin/main did.
    assert all(c[:1] != ["clone"] for c in git_calls)
    assert ["fetch", "origin"] in git_calls
    assert ["reset", "--hard", "origin/main"] in git_calls
    # READY published.
    assert any(e.type == RepoEventType.GRAPH_READY for e in publisher.events)


@pytest.mark.asyncio
async def test_partial_status_propagates_to_row_and_event(
    fake_session,
    publisher: InMemoryPublisher,
    tmp_path: Path,
) -> None:
    cfg, _repo, added, factory = fake_session
    workspace = tmp_path / "ws-7"
    (workspace / ".git").mkdir(parents=True)
    cfg.workspace_path = str(workspace)

    async def fake_run_git(*, args, cwd):
        return "abc\n" if args[:1] == ["rev-parse"] else ""

    blob = _make_blob(status_per_area=("ok", "failed"), commit_sha="abc")

    with (
        patch("agent.lifecycle.graph_refresh.async_session", new=factory),
        patch("agent.lifecycle.graph_refresh._run_git", new=fake_run_git),
        patch("agent.lifecycle.graph_refresh.run_pipeline", new=AsyncMock(return_value=blob)),
    ):
        from agent.lifecycle.graph_refresh import run_refresh

        await run_refresh(repo_id=7, request_id="r-3")

    assert added[0].status == "partial"
    ready = [e for e in publisher.events if e.type == RepoEventType.GRAPH_READY]
    assert ready[0].payload["status"] == "partial"


# ----------------------------------------------------------------------
# Failure paths
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_failure_publishes_failed(
    fake_session,
    publisher: InMemoryPublisher,
    tmp_path: Path,
) -> None:
    cfg, _repo, added, factory = fake_session
    cfg.workspace_path = str(tmp_path / "ws-7")

    async def fake_run_git(*, args, cwd):
        raise RuntimeError("clone failed: auth required")

    with (
        patch("agent.lifecycle.graph_refresh.async_session", new=factory),
        patch("agent.lifecycle.graph_refresh._run_git", new=fake_run_git),
    ):
        from agent.lifecycle.graph_refresh import run_refresh

        await run_refresh(repo_id=7, request_id="r-4")

    assert added == []  # no row written
    failed = [e for e in publisher.events if e.type == RepoEventType.GRAPH_FAILED]
    assert len(failed) == 1
    assert "clone failed" in failed[0].payload["error"]


@pytest.mark.asyncio
async def test_lock_contention_publishes_already_running(
    fake_session,
    publisher: InMemoryPublisher,
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg, _repo, added, factory = fake_session
    cfg.workspace_path = str(tmp_path / "ws-7")

    from agent.graph_workspace import GraphWorkspaceLockTimeout

    # Substitute the lock with one that immediately raises.
    async def busy_lock(*, repo_id, timeout=0):
        raise GraphWorkspaceLockTimeout(f"busy {repo_id}")

    class _BusyLockCtx:
        def __init__(self, **kw):
            self.kw = kw

        async def __aenter__(self):
            raise GraphWorkspaceLockTimeout(f"busy {self.kw['repo_id']}")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "agent.lifecycle.graph_refresh.graph_workspace_lock",
        lambda **kw: _BusyLockCtx(**kw),
    )

    with patch("agent.lifecycle.graph_refresh.async_session", new=factory):
        from agent.lifecycle.graph_refresh import run_refresh

        await run_refresh(repo_id=7, request_id="r-5")

    failed = [e for e in publisher.events if e.type == RepoEventType.GRAPH_FAILED]
    assert len(failed) == 1
    assert failed[0].payload["error"] == "analysis already running"
    # Nothing should have been added to the DB.
    assert added == []


@pytest.mark.asyncio
async def test_missing_config_is_silent(
    publisher: InMemoryPublisher,
    tmp_path: Path,
) -> None:
    """Config disappeared between request and dispatch — handler returns
    without publishing FAILED (nothing to fail on; the request is moot)."""

    class _NoCfgSession:
        async def execute(self, stmt):
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    with patch("agent.lifecycle.graph_refresh.async_session", new=_NoCfgSession):
        from agent.lifecycle.graph_refresh import run_refresh

        await run_refresh(repo_id=99, request_id="r-6")

    assert not publisher.events


@pytest.mark.asyncio
async def test_handler_event_shape_routes_to_run_refresh(
    publisher: InMemoryPublisher,
) -> None:
    """``handle(event)`` extracts repo_id + request_id and delegates."""
    captured = {}

    async def fake_run_refresh(*, repo_id, request_id):
        captured["repo_id"] = repo_id
        captured["request_id"] = request_id

    from shared.events import Event, RepoEventType

    event = Event(
        type=RepoEventType.GRAPH_REQUESTED,
        task_id=0,
        payload={"repo_id": 42, "request_id": "abc"},
    )

    with patch("agent.lifecycle.graph_refresh.run_refresh", new=fake_run_refresh):
        from agent.lifecycle.graph_refresh import handle

        await handle(event)

    assert captured == {"repo_id": 42, "request_id": "abc"}


@pytest.mark.asyncio
async def test_handler_ignores_invalid_payload(
    publisher: InMemoryPublisher,
) -> None:
    """Malformed event (no repo_id) is logged and dropped — no publish."""
    from agent.lifecycle.graph_refresh import handle
    from shared.events import Event, RepoEventType

    bad = Event(
        type=RepoEventType.GRAPH_REQUESTED,
        task_id=0,
        payload={"request_id": "x"},
    )
    await handle(bad)
    assert not publisher.events


# Silence unused-import for json in the test module (used implicitly via blob).
_ = json
