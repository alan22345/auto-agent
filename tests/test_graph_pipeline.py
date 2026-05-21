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


@pytest.mark.asyncio
class TestModuleImportResolution:
    """Bare ``module:<dotted>`` placeholders on import edges must be
    rewritten to the corresponding ``file:`` node id so the rendered
    graph has no phantom endpoints.

    Regression test for the 2026-05-21 "graph canvas empty on a freshly-
    built local repo" handover: an import edge of shape
    ``module:dispatcher.router -> module:handlers`` left phantom endpoints
    that cytoscape silently dropped (and broke its cose layout, leaving
    every node at the origin). The pipeline owns the cross-file knowledge
    needed to resolve these placeholders — the per-file parser cannot.
    """

    async def test_import_edges_resolve_module_placeholders_to_file_nodes(
        self,
        tmp_path: Path,
    ) -> None:
        # Reproduce the handover repro on a tiny fixture: a dispatcher
        # area that imports from a handlers area. Both ends must resolve
        # to ``file:`` node ids — the rendered graph cannot tolerate
        # endpoints that aren't in the node set.
        ws = tmp_path / "workspace"
        (ws / "dispatcher").mkdir(parents=True)
        (ws / "handlers").mkdir(parents=True)
        (ws / "dispatcher" / "__init__.py").write_text("")
        (ws / "dispatcher" / "router.py").write_text(
            "from handlers import ping_handler\n"
            "def dispatch(name):\n"
            "    return ping_handler(name)\n",
        )
        (ws / "handlers" / "__init__.py").write_text(
            "def ping_handler(name):\n    return name\n",
        )

        blob = await run_pipeline(workspace=str(ws), commit_sha="abc")

        node_ids = {n.id for n in blob.nodes}
        imports = [e for e in blob.edges if e.kind == "imports"]
        # The router-to-handlers import edge must exist and both
        # endpoints must be real nodes in the graph.
        router_to_handlers = [
            e
            for e in imports
            if "router" in e.evidence.file and "handlers" in e.evidence.snippet
        ]
        assert router_to_handlers, "expected the router->handlers import edge"
        for e in router_to_handlers:
            assert e.source in node_ids, (
                f"phantom source {e.source!r} — not a node in the graph"
            )
            assert e.target in node_ids, (
                f"phantom target {e.target!r} — not a node in the graph"
            )
            # And specifically they should be file-level nodes.
            assert e.source.startswith("file:")
            assert e.target.startswith("file:")

    async def test_external_module_import_edges_are_dropped(
        self,
        tmp_path: Path,
    ) -> None:
        # ``import os`` produces an edge to ``module:os`` — there is no
        # ``file:`` node for stdlib / third-party modules, so the edge
        # would render as a phantom endpoint in cytoscape. The pipeline
        # drops these so the canvas has only edges between real nodes.
        ws = tmp_path / "workspace"
        (ws / "area_a").mkdir(parents=True)
        (ws / "area_a" / "__init__.py").write_text("")
        (ws / "area_a" / "mod.py").write_text("import os\n")

        blob = await run_pipeline(workspace=str(ws), commit_sha="abc")

        node_ids = {n.id for n in blob.nodes}
        for e in blob.edges:
            assert e.source in node_ids, f"phantom source {e.source!r}"
            assert e.target in node_ids, f"phantom target {e.target!r}"


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
