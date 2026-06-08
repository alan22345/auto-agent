"""_load_or_create_row must re-run when the analyser version changes.

Before this fix the noop decision keyed solely on ``commit_sha``, so deploying
a fixed analyser never re-analysed a repo until its *source* changed. Now a
version mismatch (same commit) forces a full re-parse by clearing the
checkpoint and returning ``resume_same``.
"""

from __future__ import annotations

import pytest

from agent.lifecycle import graph_refresh as gr
from shared.models import RepoGraph


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row
        self.added: list = []

    async def execute(self, *_a, **_k):
        return _FakeResult(self._row)

    def add(self, r):
        self.added.append(r)

    async def flush(self):
        pass


def _complete_row(version: str) -> RepoGraph:
    return RepoGraph(
        repo_id=7,
        commit_sha="abc",
        analyser_version=version,
        status="ok",
        is_complete=True,
        processed_files={"pkg/x.py": 1},
        failed_sites=["site"],
        graph_json={"nodes": [1], "edges": [], "areas": [], "public_symbols": []},
    )


@pytest.mark.asyncio
async def test_noop_when_version_matches(monkeypatch) -> None:
    monkeypatch.setattr(gr, "_analyser_version", lambda: "v-1")
    _row, action = await gr._load_or_create_row(_FakeSession(_complete_row("v-1")), 7, "abc")
    assert action == "noop"


@pytest.mark.asyncio
async def test_reruns_when_version_differs(monkeypatch) -> None:
    monkeypatch.setattr(gr, "_analyser_version", lambda: "v-2")
    row, action = await gr._load_or_create_row(_FakeSession(_complete_row("v-1")), 7, "abc")
    assert action == "resume_same"  # not noop — force re-analysis
    assert row.analyser_version == "v-2"
    assert row.is_complete is False
    assert row.processed_files == {}  # checkpoint cleared → full re-parse
    assert row.failed_sites == []
    assert row.graph_json.get("nodes") == []
