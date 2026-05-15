"""Tree-sitter Python parser tests (ADR-016 Phase 2).

The parser is the deterministic spine — these tests pin behaviour the
pipeline assumes:

* nodes for files / classes / functions (incl. methods) at the right
  hierarchy levels;
* ``imports`` edges for both ``import`` and ``from ... import`` (absolute
  + relative) forms;
* ``inherits`` edges for classes whose parent is in module scope;
* ``calls`` edges for *statically* resolvable callees only;
* dynamic-dispatch sites counted via
  ``ParseResult.unresolved_dynamic_sites`` but **not** emitted as edges;
* every emitted edge carries ``source_kind="ast"`` and real evidence.
"""

from __future__ import annotations

from agent.graph_analyzer.parsers.python import PythonParser


def _parse(text: str, *, rel_path: str = "pkg/mod.py", area: str = "pkg"):
    return PythonParser().parse_file(
        rel_path=rel_path,
        area=area,
        source=text.encode(),
    )


class TestNodes:
    def test_emits_file_node(self) -> None:
        result = _parse("x = 1\n")
        files = [n for n in result.nodes if n.kind == "file"]
        assert len(files) == 1
        assert files[0].id == "file:pkg/mod.py"
        assert files[0].area == "pkg"
        assert files[0].parent == "area:pkg"

    def test_emits_class_and_methods(self) -> None:
        result = _parse(
            "class Foo:\n    def bar(self): pass\n    def baz(self): pass\n",
        )
        classes = [n for n in result.nodes if n.kind == "class"]
        assert [c.label for c in classes] == ["Foo"]
        funcs = [n for n in result.nodes if n.kind == "function"]
        labels = [f.label for f in funcs]
        assert "Foo.bar" in labels
        assert "Foo.baz" in labels
        # Method parent is the class id.
        assert funcs[0].parent == "pkg/mod.py::Foo"

    def test_emits_top_level_function(self) -> None:
        result = _parse("def top():\n    pass\n")
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert [f.label for f in funcs] == ["top"]
        assert funcs[0].parent == "file:pkg/mod.py"


class TestImports:
    def test_simple_import(self) -> None:
        result = _parse("import os\n")
        imports = [e for e in result.edges if e.kind == "imports"]
        assert len(imports) == 1
        e = imports[0]
        assert e.target == "module:os"
        assert e.source_kind == "ast"
        assert e.evidence.snippet == "import os"
        assert e.evidence.line == 1

    def test_from_absolute_import(self) -> None:
        result = _parse(
            "from agent.graph import thing\n",
            rel_path="orchestrator/router.py",
            area="orchestrator",
        )
        imports = [e for e in result.edges if e.kind == "imports"]
        assert len(imports) == 1
        e = imports[0]
        assert e.target == "module:agent.graph"
        assert e.source == "module:orchestrator.router"

    def test_relative_import_two_dots(self) -> None:
        # `from ..base import X` inside agent_area/sub/relative.py
        # resolves to `agent_area.base`.
        result = _parse(
            "from ..base import Animal\n",
            rel_path="agent_area/sub/relative.py",
            area="agent_area",
        )
        imports = [e for e in result.edges if e.kind == "imports"]
        assert imports[0].target == "module:agent_area.base"


class TestInherits:
    def test_resolves_imported_parent(self) -> None:
        result = _parse(
            "from agent_area.base import Animal\nclass Dog(Animal):\n    pass\n",
        )
        inherits = [e for e in result.edges if e.kind == "inherits"]
        assert len(inherits) == 1
        e = inherits[0]
        assert e.source == "pkg/mod.py::Dog"
        assert e.target == "module:agent_area.base.Animal"
        assert e.source_kind == "ast"
        assert e.evidence.line == 2

    def test_resolves_same_module_parent(self) -> None:
        result = _parse(
            "class Base:\n    pass\nclass Child(Base):\n    pass\n",
        )
        inherits = [e for e in result.edges if e.kind == "inherits"]
        assert len(inherits) == 1
        assert inherits[0].target == "pkg/mod.py::Base"

    def test_attribute_parent_counts_as_dynamic(self) -> None:
        result = _parse(
            "import mod\nclass X(mod.Base):\n    pass\n",
        )
        assert all(e.kind != "inherits" for e in result.edges)
        assert result.unresolved_dynamic_sites >= 1


class TestCalls:
    def test_resolves_imported_function_call(self) -> None:
        result = _parse(
            "from agent_area.base import helper\ndef top():\n    return helper()\n",
        )
        calls = [e for e in result.edges if e.kind == "calls"]
        assert len(calls) == 1
        e = calls[0]
        assert e.source == "pkg/mod.py::top"
        assert e.target == "module:agent_area.base.helper"
        assert e.source_kind == "ast"
        assert e.evidence.snippet == "return helper()"

    def test_resolves_self_method_call(self) -> None:
        result = _parse(
            "class Foo:\n"
            "    def bar(self):\n"
            "        return self.baz()\n"
            "    def baz(self):\n"
            "        return 1\n",
        )
        calls = [e for e in result.edges if e.kind == "calls"]
        assert len(calls) == 1
        e = calls[0]
        assert e.source == "pkg/mod.py::Foo.bar"
        assert e.target == "pkg/mod.py::Foo.baz"

    def test_resolves_class_instantiation(self) -> None:
        result = _parse(
            "class Foo:\n    pass\ndef make():\n    return Foo()\n",
        )
        calls = [e for e in result.edges if e.kind == "calls"]
        assert any(e.source == "pkg/mod.py::make" and e.target == "pkg/mod.py::Foo" for e in calls)

    def test_dynamic_dispatch_site_counted_not_resolved(self) -> None:
        # Registry pattern — HANDLERS[name](payload) is dynamic.
        result = _parse(
            "HANDLERS = {}\ndef dispatch(name, payload):\n    return HANDLERS[name](payload)\n",
        )
        # No edges for the dynamic call.
        calls = [e for e in result.edges if e.kind == "calls"]
        assert calls == []
        # And the dynamic site is counted.
        assert result.unresolved_dynamic_sites >= 1


class TestEvidence:
    def test_every_edge_has_ast_source_kind_and_real_snippet(self) -> None:
        result = _parse(
            "import os\n"
            "class Base:\n    pass\n"
            "class Child(Base):\n"
            "    def m(self):\n        return os\n",
        )
        assert all(e.source_kind == "ast" for e in result.edges)
        for e in result.edges:
            assert e.evidence.file == "pkg/mod.py"
            assert e.evidence.line >= 1
            assert e.evidence.snippet  # non-empty


class TestMalformed:
    def test_does_not_crash_on_syntax_error(self) -> None:
        # Tree-sitter's error recovery means a syntax error gives back a
        # tree with ERROR nodes; the parser must not raise.
        result = _parse("def broken(:\n    pass\n")
        # We still emit a file node at minimum.
        assert any(n.kind == "file" for n in result.nodes)


class TestUnresolvedSites:
    """Phase 3 (ADR-016 §3) — the parser exposes unresolved dispatch sites,
    not just a count. Each site carries enough context for the LLM
    gap-fill stage to attempt resolution without re-reading the file."""

    def test_registry_dispatch_emits_site_with_registry_hint(self) -> None:
        result = _parse(
            "HANDLERS = {}\n"
            "def dispatch(name, payload):\n"
            "    return HANDLERS[name](payload)\n",
        )
        sites = result.unresolved_sites
        assert sites, "expected at least one unresolved site"
        site = sites[0]
        assert site.file == "pkg/mod.py"
        assert site.line == 3
        assert "HANDLERS[name](payload)" in site.snippet
        assert site.containing_node_id == "pkg/mod.py::dispatch"
        assert site.pattern_hint == "registry"
        # Surrounding window includes the dispatch function header.
        assert "def dispatch" in site.surrounding_code

    def test_attribute_call_on_imported_module_emits_dict_call_hint(self) -> None:
        result = _parse(
            "import os\n"
            "def go():\n"
            "    return os.getenv('X')\n",
        )
        sites = result.unresolved_sites
        assert any(s.pattern_hint == "dict_call" for s in sites)
        attr_site = next(s for s in sites if s.pattern_hint == "dict_call")
        assert attr_site.containing_node_id == "pkg/mod.py::go"
        assert "os.getenv" in attr_site.snippet

    def test_self_unknown_method_emits_unknown_hint(self) -> None:
        result = _parse(
            "class Foo:\n"
            "    def caller(self):\n"
            "        return self.unknown_method()\n",
        )
        sites = result.unresolved_sites
        assert sites
        # Should be classified as ``unknown`` (we have no way to know
        # whether ``unknown_method`` is inherited or fabricated).
        assert any(
            s.pattern_hint == "unknown"
            and s.containing_node_id == "pkg/mod.py::Foo.caller"
            for s in sites
        )

    def test_attribute_class_inheritance_emits_site(self) -> None:
        result = _parse(
            "import mod\n"
            "class X(mod.Base):\n"
            "    pass\n",
        )
        sites = result.unresolved_sites
        assert sites
        # The containing node for an inheritance site is the class id.
        site = sites[0]
        assert site.containing_node_id == "pkg/mod.py::X"
        assert "class X" in site.snippet or "X(mod.Base)" in site.snippet

    def test_unbound_identifier_call_emits_unknown_hint(self) -> None:
        result = _parse(
            "def caller():\n"
            "    return mystery()\n",
        )
        sites = result.unresolved_sites
        assert sites
        site = sites[0]
        assert site.pattern_hint == "unknown"
        assert site.containing_node_id == "pkg/mod.py::caller"
        assert "mystery()" in site.snippet

    def test_getattr_dispatch_emits_getattr_hint(self) -> None:
        result = _parse(
            "def dispatch(obj, name):\n"
            "    return getattr(obj, name)(42)\n",
        )
        sites = result.unresolved_sites
        assert any(s.pattern_hint == "getattr" for s in sites)

    def test_unresolved_dynamic_sites_count_matches_list_length(self) -> None:
        result = _parse(
            "HANDLERS = {}\n"
            "def go(n):\n"
            "    return HANDLERS[n]()\n",
        )
        assert result.unresolved_dynamic_sites == len(result.unresolved_sites)
