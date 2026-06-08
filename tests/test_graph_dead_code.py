"""Unit tests for agent.graph_analyzer.dead_code.compute_dead_code.

All tests use hand-built RepoGraphBlob instances — no I/O, no LLM, fast.

Import-edge target representation confirmed from pipeline.py
_resolve_module_imports_to_files (~line 817): after resolution, first-party
imports edges target ``file:<path>`` node ids (not ``module:`` ids).
The function builds file_module_to_id and rewrites ``module:<dotted>``
endpoints to ``n.id`` of the matching ``kind="file"`` node.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.graph_analyzer.dead_code import compute_dead_code
from shared.types import (
    AreaStatus,
    DeadCodeFinding,
    Edge,
    EdgeEvidence,
    Node,
    RepoGraphBlob,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUMMY_SHA = "deadbeef"
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_DUMMY_EVIDENCE = EdgeEvidence(file="x.py", line=1, snippet="import foo")


def _blob(
    nodes: list[Node],
    edges: list[Edge],
    public_symbols: list[str] | None = None,
) -> RepoGraphBlob:
    """Construct a minimal RepoGraphBlob for testing."""
    return RepoGraphBlob(
        commit_sha=_DUMMY_SHA,
        generated_at=_NOW,
        analyser_version="test",
        areas=[AreaStatus(name="test_area", status="ok")],
        nodes=nodes,
        edges=edges,
        public_symbols=public_symbols or [],
    )


def _file_node(file_path: str, area: str = "test_area") -> Node:
    """Create a ``kind="file"`` node for *file_path*."""
    return Node(
        id=f"file:{file_path}",
        kind="file",
        label=file_path.rsplit("/", 1)[-1],
        file=file_path,
        area=area,
    )


def _func_node(
    node_id: str,
    file_path: str,
    decorators: list[str] | None = None,
    area: str = "test_area",
) -> Node:
    """Create a ``kind="function"`` node."""
    return Node(
        id=node_id,
        kind="function",
        label=node_id.split("::")[-1],
        file=file_path,
        area=area,
        decorators=decorators or [],
    )


def _class_node(node_id: str, file_path: str, area: str = "test_area") -> Node:
    """Create a ``kind="class"`` node."""
    return Node(
        id=node_id,
        kind="class",
        label=node_id.split("::")[-1],
        file=file_path,
        area=area,
    )


def _calls_edge(source: str, target: str) -> Edge:
    return Edge(
        source=source,
        target=target,
        kind="calls",
        evidence=_DUMMY_EVIDENCE,
        source_kind="ast",
    )


def _inherits_edge(source: str, target: str) -> Edge:
    return Edge(
        source=source,
        target=target,
        kind="inherits",
        evidence=_DUMMY_EVIDENCE,
        source_kind="ast",
    )


def _imports_edge(source_file_id: str, target_file_id: str) -> Edge:
    """Create an imports edge between two ``file:`` node ids (post-resolution form)."""
    return Edge(
        source=source_file_id,
        target=target_file_id,
        kind="imports",
        evidence=_DUMMY_EVIDENCE,
        source_kind="ast",
    )


# ---------------------------------------------------------------------------
# unused_export tests
# ---------------------------------------------------------------------------


class TestUnusedExport:
    def test_exported_function_no_external_caller_is_flagged(self):
        """An exported function with no calls/inherits from outside its file → unused_export."""
        fn = _func_node("a.py::helper", "a.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=["a.py::helper"])
        findings = compute_dead_code(blob)
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == "unused_export"
        assert f.target == "a.py::helper"
        assert f.file == "a.py"

    def test_exported_function_with_external_calls_edge_is_not_flagged(self):
        """An exported function that has an incoming calls edge from a different file → not flagged."""
        fn_helper = _func_node("a.py::helper", "a.py")
        fn_caller = _func_node("b.py::caller", "b.py")
        edge = _calls_edge("b.py::caller", "a.py::helper")
        blob = _blob(
            nodes=[fn_helper, fn_caller],
            edges=[edge],
            public_symbols=["a.py::helper"],
        )
        findings = compute_dead_code(blob)
        assert not any(f.target == "a.py::helper" for f in findings)

    def test_exported_decorated_function_with_no_caller_is_not_flagged(self):
        """Decorated exported functions are skipped — decorators imply runtime wiring."""
        fn = _func_node("a.py::handler", "a.py", decorators=["@app.route('/x')"])
        blob = _blob(nodes=[fn], edges=[], public_symbols=["a.py::handler"])
        findings = compute_dead_code(blob)
        assert not any(f.target == "a.py::handler" for f in findings)

    def test_exported_entry_point_function_is_not_flagged(self):
        """A function that is an entry point (e.g. Celery task) → not flagged even if unused."""
        fn = _func_node("a.py::my_worker", "a.py")
        # my_worker triggers _QUEUE_NAME_RE in entry_points.py → detected as queue entry point.
        blob = _blob(nodes=[fn], edges=[], public_symbols=["a.py::my_worker"])
        findings = compute_dead_code(blob)
        assert not any(f.target == "a.py::my_worker" for f in findings)

    def test_same_file_caller_does_not_rescue_export(self):
        """A calls edge from a node in the SAME file does not count as an external caller."""
        fn_helper = _func_node("a.py::helper", "a.py")
        fn_internal = _func_node("a.py::internal", "a.py")
        edge = _calls_edge("a.py::internal", "a.py::helper")
        blob = _blob(
            nodes=[fn_helper, fn_internal],
            edges=[edge],
            public_symbols=["a.py::helper"],
        )
        findings = compute_dead_code(blob)
        assert any(f.target == "a.py::helper" and f.kind == "unused_export" for f in findings)

    def test_class_with_external_inherits_edge_is_not_flagged(self):
        """A class with an inherits edge from a different file → not flagged."""
        base_cls = _class_node("base.py::Base", "base.py")
        child_cls = _class_node("child.py::Child", "child.py")
        edge = _inherits_edge("child.py::Child", "base.py::Base")
        blob = _blob(
            nodes=[base_cls, child_cls],
            edges=[edge],
            public_symbols=["base.py::Base"],
        )
        findings = compute_dead_code(blob)
        assert not any(f.target == "base.py::Base" for f in findings)

    def test_class_no_external_subclass_is_flagged(self):
        """An exported class with no external subclass or caller → flagged."""
        cls = _class_node("base.py::Orphan", "base.py")
        blob = _blob(nodes=[cls], edges=[], public_symbols=["base.py::Orphan"])
        findings = compute_dead_code(blob)
        assert any(f.target == "base.py::Orphan" and f.kind == "unused_export" for f in findings)

    def test_non_function_class_node_skipped(self):
        """Nodes that are neither function nor class are never flagged unused_export."""
        file_node = _file_node("a.py")
        blob = _blob(nodes=[file_node], edges=[], public_symbols=["file:a.py"])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_export" for f in findings)

    def test_http_decorated_entry_point_not_flagged(self):
        """A FastAPI route handler decorated with @router.get → entry point, not flagged."""
        fn = _func_node("api.py::get_users", "api.py", decorators=['@router.get("/users")'])
        blob = _blob(nodes=[fn], edges=[], public_symbols=["api.py::get_users"])
        findings = compute_dead_code(blob)
        # Decorated + entry point → excluded
        assert not any(f.target == "api.py::get_users" for f in findings)


# ---------------------------------------------------------------------------
# unused_file tests
# ---------------------------------------------------------------------------


class TestUnusedFile:
    def test_file_no_incoming_imports_and_no_entry_point_is_flagged(self):
        """A file node with no incoming imports and no entry-point nodes → unused_file."""
        fn = _file_node("legacy.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == "unused_file"
        assert f.target == "file:legacy.py"
        assert f.file == "legacy.py"

    def test_file_with_incoming_import_is_not_flagged(self):
        """A file that is imported by another file → not flagged."""
        src = _file_node("a.py")
        tgt = _file_node("b.py")
        edge = _imports_edge("file:a.py", "file:b.py")
        blob = _blob(nodes=[src, tgt], edges=[edge], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.target == "file:b.py" for f in findings)

    def test_init_py_not_flagged(self):
        """__init__.py files are excluded from unused_file detection."""
        fn = _file_node("pkg/__init__.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_main_py_not_flagged(self):
        """main.py is excluded from unused_file detection."""
        fn = _file_node("main.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_dunder_main_py_not_flagged(self):
        """__main__.py is excluded from unused_file detection."""
        fn = _file_node("pkg/__main__.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_test_py_prefix_not_flagged(self):
        """test_*.py files are excluded from unused_file detection."""
        fn = _file_node("tests/test_foo.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_test_py_suffix_not_flagged(self):
        """*_test.py files are excluded from unused_file detection."""
        fn = _file_node("tests/foo_test.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_spec_ts_not_flagged(self):
        """*.spec.ts files are excluded from unused_file detection."""
        fn = _file_node("frontend/Foo.spec.ts")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_test_ts_not_flagged(self):
        """*.test.ts files are excluded from unused_file detection."""
        fn = _file_node("frontend/Foo.test.ts")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_file_containing_entry_point_not_flagged(self):
        """A file that contains an entry-point node is NOT flagged as unused_file."""
        file_node = _file_node("worker.py")
        # worker node triggers queue detection via name pattern *_worker
        worker_fn = _func_node("worker.py::run_worker", "worker.py")
        blob = _blob(nodes=[file_node, worker_fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.target == "file:worker.py" for f in findings)

    def test_conftest_py_not_flagged(self):
        """conftest.py is excluded from unused_file detection."""
        fn = _file_node("tests/conftest.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_app_py_not_flagged(self):
        """app.py is excluded from unused_file detection."""
        fn = _file_node("app.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_run_py_not_flagged(self):
        """run.py is excluded from unused_file detection."""
        fn = _file_node("run.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_setup_py_not_flagged(self):
        """setup.py is excluded from unused_file detection."""
        fn = _file_node("setup.py")
        blob = _blob(nodes=[fn], edges=[], public_symbols=[])
        findings = compute_dead_code(blob)
        assert not any(f.kind == "unused_file" for f in findings)

    def test_file_imported_via_unresolved_module_target_not_flagged(self):
        """A file imported via an unresolved ``module:`` imports edge must NOT be flagged.

        Scenario: the pipeline normally resolves ``module:pkg.b`` → ``file:pkg/b.py``
        before calling compute_dead_code.  When called with a still-unresolved edge
        (edge.target == "module:pkg.b"), the function must map it to the
        corresponding file node (``file:pkg/b.py``) and NOT flag ``pkg/b.py`` as
        unused_file.
        """
        # file:pkg/b.py — the file node that would normally be the resolved target
        file_b = _file_node("pkg/b.py", area="pkg")
        # file:pkg/a.py — the importer
        file_a = _file_node("pkg/a.py", area="pkg")
        # An imports edge with an UNRESOLVED module: target (pre-resolution form)
        unresolved_imports_edge = Edge(
            source="file:pkg/a.py",
            target="module:pkg.b",
            kind="imports",
            evidence=_DUMMY_EVIDENCE,
            source_kind="ast",
        )
        blob = _blob(
            nodes=[file_a, file_b],
            edges=[unresolved_imports_edge],
            public_symbols=[],
        )
        findings = compute_dead_code(blob)
        unused_file_targets = {f.target for f in findings if f.kind == "unused_file"}
        assert "file:pkg/b.py" not in unused_file_targets, (
            "pkg/b.py was falsely flagged as unused_file even though it is imported "
            "via an unresolved 'module:pkg.b' edge. "
            f"unused_file findings: {unused_file_targets}"
        )


# ---------------------------------------------------------------------------
# Determinism tests
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_calls_produce_identical_sorted_output(self):
        """compute_dead_code must be deterministic across multiple calls."""
        fn_a = _func_node("z.py::z_func", "z.py")
        fn_b = _func_node("a.py::a_func", "a.py")
        file_z = _file_node("z.py")
        file_a = _file_node("a.py")
        blob = _blob(
            nodes=[fn_a, fn_b, file_z, file_a],
            edges=[],
            public_symbols=["z.py::z_func", "a.py::a_func"],
        )
        first = compute_dead_code(blob)
        second = compute_dead_code(blob)
        assert first == second
        # Sorted by (kind, target) — verify the sort key is respected
        sort_keys = [(f.kind, f.target) for f in first]
        assert sort_keys == sorted(sort_keys)

    def test_no_duplicates(self):
        """Each symbol or file appears at most once in the output."""
        fn = _func_node("a.py::helper", "a.py")
        file_node = _file_node("legacy.py")
        blob = _blob(
            nodes=[fn, file_node],
            edges=[],
            public_symbols=["a.py::helper"],
        )
        findings = compute_dead_code(blob)
        targets = [f.target for f in findings]
        assert len(targets) == len(set(targets))

    def test_returns_list_of_dead_code_finding(self):
        """Return type is list[DeadCodeFinding]."""
        blob = _blob(nodes=[], edges=[], public_symbols=[])
        result = compute_dead_code(blob)
        assert isinstance(result, list)
        for f in result:
            assert isinstance(f, DeadCodeFinding)


class TestImportAsUsageAndTestRefs:
    """Fix F (symbol imported by name = used) and Fix B (test-only refs)."""

    def test_unused_export_suppressed_when_symbol_imported_by_name(self):
        # F: a public symbol imported by name from another file counts as
        # used, even with no call/inherit edge (sidesteps call-graph gaps).
        nodes = [
            _file_node("pkg/exporter.py"),
            _file_node("pkg/consumer.py"),
            _func_node("pkg/exporter.py::thing", "pkg/exporter.py"),
        ]
        blob = _blob(nodes, edges=[], public_symbols=["pkg/exporter.py::thing"])
        findings = compute_dead_code(
            blob,
            production_imports=[("pkg/consumer.py", "module:pkg.exporter.thing")],
        )
        targets = {f.target for f in findings if f.kind == "unused_export"}
        assert "pkg/exporter.py::thing" not in targets

    def test_unused_export_relabeled_when_only_test_imports(self):
        # B: referenced only by tests → still listed, but labelled as such.
        nodes = [
            _file_node("pkg/exporter.py"),
            _func_node("pkg/exporter.py::thing", "pkg/exporter.py"),
        ]
        blob = _blob(nodes, edges=[], public_symbols=["pkg/exporter.py::thing"])
        findings = compute_dead_code(
            blob,
            test_imports=[("tests/test_x.py", "module:pkg.exporter.thing")],
        )
        f = next(f for f in findings if f.target == "pkg/exporter.py::thing")
        assert f.kind == "unused_export"
        assert "test" in f.reason.lower()

    def test_unused_file_relabeled_when_only_test_imports(self):
        nodes = [_file_node("pkg/helper.py")]
        blob = _blob(nodes, edges=[], public_symbols=[])
        findings = compute_dead_code(
            blob,
            test_imports=[("tests/test_x.py", "module:pkg.helper")],
        )
        f = next(
            f
            for f in findings
            if f.kind == "unused_file" and f.target == "file:pkg/helper.py"
        )
        assert "test" in f.reason.lower()

    def test_production_import_beats_test_import_for_file(self):
        # Imported by BOTH prod and test → plain "used", not flagged at all.
        nodes = [_file_node("pkg/helper.py"), _file_node("pkg/app.py")]
        blob = _blob(nodes, edges=[
            Edge(source="file:pkg/app.py", target="file:pkg/helper.py",
                 kind="imports", evidence=_DUMMY_EVIDENCE, source_kind="ast"),
        ], public_symbols=[])
        findings = compute_dead_code(
            blob,
            test_imports=[("tests/test_x.py", "module:pkg.helper")],
        )
        assert "file:pkg/helper.py" not in {f.target for f in findings}
