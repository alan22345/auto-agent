"""Tests for eval/providers/graph_support.py — graph-enabled eval arm (ADR-025).

The eval A/B needs the graph-on arm to have a real, queryable graph for
its temp fixture workspace without Postgres: build an AST-only blob via
run_pipeline, then patch the two DB seams (the tool's _load_graph and
the nudge gate) for the duration of the agent run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path  # noqa: TC003

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "eval", "providers"))

from graph_support import build_graph_blob, graph_enabled  # noqa: E402


def _make_fixture_repo(tmp_path: Path) -> str:
    ws = tmp_path / "ws"
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "__init__.py").write_text("")
    (ws / "app" / "helpers.py").write_text(
        "def slugify(text):\n    return text.lower().replace(' ', '-')\n"
    )
    (ws / "app" / "main.py").write_text(
        "from app.helpers import slugify\n\n\ndef handle(name):\n    return slugify(name)\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(
        ["git", "-c", "user.email=e@v.al", "-c", "user.name=eval", "commit", "-q", "-m", "init"],
        cwd=ws,
        check=True,
    )
    return str(ws)


@pytest.mark.asyncio
async def test_build_graph_blob_produces_nodes_and_edges(tmp_path: Path) -> None:
    ws = _make_fixture_repo(tmp_path)

    blob, sha = await build_graph_blob(ws)

    node_ids = {n.id for n in blob.nodes}
    assert any("slugify" in node_id for node_id in node_ids)
    assert any("handle" in node_id for node_id in node_ids)
    assert len(sha) == 40
    assert blob.commit_sha == sha


@pytest.mark.asyncio
async def test_graph_enabled_lets_the_tool_answer_without_db(tmp_path: Path) -> None:
    ws = _make_fixture_repo(tmp_path)
    blob, sha = await build_graph_blob(ws)

    from agent.tools.base import ToolContext
    from agent.tools.query_repo_graph import QueryRepoGraphTool

    with graph_enabled(ws, blob, sha):
        result = await QueryRepoGraphTool().execute(
            {"repo_id": 1, "op": "search_symbols", "params": {"query": "slugify"}},
            ToolContext(workspace=ws),
        )

    assert result.is_error is False
    assert "slugify" in result.output
    assert '"drifted": false' in result.output.lower()


@pytest.mark.asyncio
async def test_graph_enabled_fires_the_nudge_without_db(tmp_path: Path) -> None:
    ws = _make_fixture_repo(tmp_path)
    blob, sha = await build_graph_blob(ws)

    from agent.context.system import SystemPromptBuilder

    with graph_enabled(ws, blob, sha):
        prompt = await SystemPromptBuilder().build(ws, repo_id=1)

    assert "query_repo_graph" in prompt
    assert "search_symbols" in prompt
