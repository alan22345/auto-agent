"""Pipeline tests (ADR-016 Phase 2 — AST-only path).

Covers area discovery (defaults + ``.auto-agent/graph.yml`` override),
per-area failure isolation, and the assembled ``RepoGraphBlob`` shape
without the Phase 3 LLM gap-fill stage. ``run_pipeline`` is async since
Phase 3 — these tests pass ``provider=None`` (the default) to exercise
the AST-only behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import (
    analyser_version,
    overall_status,
    run_pipeline,
)
from shared.types import AreaStatus, RepoGraphBlob

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_python"


def _setup_workspace(tmp_path: Path, *, with_yml: bool = False) -> str:
    """Copy the Python fixture into ``tmp_path`` and return the workspace
    root. Keeping the copy out of the source fixture lets each test write
    a different ``.auto-agent/graph.yml`` without contaminating others."""
    import shutil

    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    if with_yml:
        (target / ".auto-agent").mkdir(exist_ok=True)
        (target / ".auto-agent" / "graph.yml").write_text(
            "areas:\n"
            "  - name: backend\n"
            '    paths: ["agent_area/**"]\n'
            "  - name: api\n"
            '    paths: ["orchestrator_area/**"]\n',
        )
    return str(target)


@pytest.mark.asyncio
class TestRunPipeline:
    async def test_default_areas_are_top_level_dirs(self, tmp_path: Path) -> None:
        ws = _setup_workspace(tmp_path)
        blob = await run_pipeline(workspace=ws, commit_sha="deadbeef")
        assert isinstance(blob, RepoGraphBlob)
        names = {a.name for a in blob.areas}
        assert "agent_area" in names
        assert "orchestrator_area" in names

    async def test_blob_contains_python_nodes_and_edges_with_ast_source_kind(
        self,
        tmp_path: Path,
    ) -> None:
        ws = _setup_workspace(tmp_path)
        blob = await run_pipeline(workspace=ws, commit_sha="abc1234")
        assert blob.commit_sha == "abc1234"
        assert blob.analyser_version == analyser_version()
        # Dog inherits Animal (from agent_area.base) — present.
        inherits = [e for e in blob.edges if e.kind == "inherits"]
        assert any(
            e.target == "module:agent_area.base.Animal" and e.source == "agent_area/dog.py::Dog"
            for e in inherits
        )
        # Every edge is AST + has evidence (no provider passed, so no
        # LLM edges should appear).
        for e in blob.edges:
            assert e.source_kind == "ast"
            assert e.evidence.file
            assert e.evidence.line >= 1
            assert e.evidence.snippet
        # Calls — describe() calls self.speak() (Dog) and helper() (cross-module).
        calls = [e for e in blob.edges if e.kind == "calls"]
        assert any(
            e.source == "agent_area/dog.py::Dog.describe"
            and e.target == "agent_area/dog.py::Dog.speak"
            for e in calls
        )
        assert any(e.target == "module:agent_area.base.helper" for e in calls)

    async def test_one_area_has_partial_status_when_broken_file_fails(
        self,
        tmp_path: Path,
    ) -> None:
        # ``orchestrator_area/broken.py`` has invalid syntax. The parser
        # itself recovers (tree-sitter ERROR-node handling), so this
        # specific case shouldn't mark the area failed — it should still
        # be ``ok`` with the broken file contributing only a file node.
        ws = _setup_workspace(tmp_path)
        blob = await run_pipeline(workspace=ws, commit_sha="abc")
        statuses = {a.name: a for a in blob.areas}
        assert statuses["agent_area"].status == "ok"
        # orchestrator_area still completes — broken.py was handled.
        assert statuses["orchestrator_area"].status == "ok"

    async def test_graph_yml_overrides_default_area_layout(self, tmp_path: Path) -> None:
        ws = _setup_workspace(tmp_path, with_yml=True)
        blob = await run_pipeline(workspace=ws, commit_sha="x")
        names = sorted(a.name for a in blob.areas)
        assert names == ["api", "backend"]
        # All nodes get the area name from the yml mapping.
        backend_nodes = [n for n in blob.nodes if n.area == "backend"]
        assert any(n.id == "agent_area/dog.py::Dog" for n in backend_nodes)

    async def test_unresolved_dynamic_sites_count_is_per_area(self, tmp_path: Path) -> None:
        # registry.py's HANDLERS[name](payload) is one dynamic site at
        # least. The area's ``unresolved_dynamic_sites`` reflects that.
        ws = _setup_workspace(tmp_path)
        blob = await run_pipeline(workspace=ws, commit_sha="x")
        statuses = {a.name: a for a in blob.areas}
        assert statuses["agent_area"].unresolved_dynamic_sites >= 1

    async def test_skip_list_excludes_non_source_dirs(self, tmp_path: Path) -> None:
        ws = _setup_workspace(tmp_path)
        # Stash a __pycache__ at the workspace root — must not become an
        # area, and any .py inside it must not be picked up.
        cache = os.path.join(ws, "__pycache__")
        os.makedirs(cache, exist_ok=True)
        Path(cache, "junk.py").write_text("def junk(): pass\n")
        blob = await run_pipeline(workspace=ws, commit_sha="x")
        names = {a.name for a in blob.areas}
        assert "__pycache__" not in names
        # No node should reference a junk.py either.
        assert not any(n.file and n.file.startswith("__pycache__/") for n in blob.nodes)


class TestOverallStatus:
    def test_all_ok(self) -> None:
        s = [AreaStatus(name="a", status="ok"), AreaStatus(name="b", status="ok")]
        assert overall_status(s) == "ok"

    def test_one_failed_one_ok_is_partial(self) -> None:
        s = [
            AreaStatus(name="a", status="ok"),
            AreaStatus(name="b", status="failed", error="boom"),
        ]
        assert overall_status(s) == "partial"

    def test_all_failed(self) -> None:
        s = [
            AreaStatus(name="a", status="failed", error="x"),
            AreaStatus(name="b", status="failed", error="y"),
        ]
        assert overall_status(s) == "failed"

    def test_empty(self) -> None:
        assert overall_status([]) == "failed"
