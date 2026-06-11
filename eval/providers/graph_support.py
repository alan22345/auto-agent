"""Give the eval agent a real code graph without Postgres (ADR-025).

The A/B eval's graph-on arm needs ``query_repo_graph`` to work against
the temp fixture workspace. In production the tool reads
``RepoGraphConfig``/``RepoGraph`` rows; in eval there is no database,
so this module builds an AST-only blob with the production pipeline
and patches the two DB seams for the duration of the agent run:

* ``agent.tools.query_repo_graph._load_graph`` — returns an in-memory
  config + graph row for the fixture workspace.
* ``SystemPromptBuilder._has_active_code_graph`` — gates the nudge.

Staleness needs no patching: the analyser workspace *is* the task
workspace (same HEAD → fresh) and the fixture has no origin remote, so
``compute_staleness`` falls back to the workspace comparison.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

EVAL_REPO_ID = 1


async def build_graph_blob(workspace: str):
    """Run the production analyser (AST-only) over ``workspace``.

    Returns ``(RepoGraphBlob, head_sha)``. No LLM provider is passed,
    so gap-fill is skipped — edges are the deterministic tree-sitter
    set, which is what fixture-sized repos need.
    """
    from agent.graph_analyzer.pipeline import run_pipeline

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    blob = await run_pipeline(workspace=workspace, commit_sha=sha, provider=None)
    return blob, sha


@contextlib.contextmanager
def graph_enabled(workspace: str, blob, sha: str):
    """Patch the DB seams so the graph tool + nudge see an active graph."""
    import agent.context.system as system_module
    import agent.tools.query_repo_graph as tool_module

    branch = _current_branch(workspace)
    config = SimpleNamespace(
        repo_id=EVAL_REPO_ID,
        workspace_path=workspace,
        last_analysis_id=1,
        analysis_branch=branch,
    )
    graph_row = SimpleNamespace(
        id=1,
        commit_sha=sha,
        graph_json=blob.model_dump(mode="json"),
        flow_json=None,
    )

    async def _load_graph_stub(_repo_id: int):
        return config, graph_row

    async def _graph_always_active(self, _repo_id: int) -> bool:
        return True

    original_load = tool_module._load_graph
    original_gate = system_module.SystemPromptBuilder._has_active_code_graph
    tool_module._load_graph = _load_graph_stub
    system_module.SystemPromptBuilder._has_active_code_graph = _graph_always_active
    try:
        yield
    finally:
        tool_module._load_graph = original_load
        system_module.SystemPromptBuilder._has_active_code_graph = original_gate


def _current_branch(workspace: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "main"


__all__ = ["EVAL_REPO_ID", "build_graph_blob", "graph_enabled"]


if __name__ == "__main__":
    # Smoke: build a blob for the workspace given as argv[1].
    ws = sys.argv[1]
    b, s = asyncio.run(build_graph_blob(ws))
    print(f"{len(b.nodes)} nodes, {len(b.edges)} edges @ {s[:8]}")
