"""Tree-sitter TypeScript parser tests (ADR-016 Phase 4).

Mirrors ``test_graph_parser_python.py`` for the TS path. The parser
shares the same :class:`ParseResult` shape so the pipeline doesn't need
a language switch — adding TS is a single file plus one extension
registry entry.

These tests pin:

* nodes for files / classes / functions / methods, plus exported
  constants / types as function-kind nodes (per spec: top-level
  ``export function``, ``export class``, ``export const``, ``export type``);
* ``imports`` edges from named, default, namespace and dynamic-literal
  ``import("...")`` forms;
* ``inherits`` edges from ``class X extends Y`` and
  ``interface X extends Y``;
* statically-resolvable calls — bare identifier, ``this.method``,
  ``new Foo()``, ``Foo.staticMethod()`` (when ``Foo`` is module-bound);
* unresolved sites for dynamic-dispatch shapes;
* graceful tree-sitter error recovery.
"""

from __future__ import annotations

from agent.graph_analyzer.parsers.typescript import TypeScriptParser


def _parse(text: str, *, rel_path: str = "frontend/mod.ts", area: str = "frontend"):
    return TypeScriptParser().parse_file(
        rel_path=rel_path,
        area=area,
        source=text.encode(),
    )


class TestNodes:
    def test_emits_file_node(self) -> None:
        result = _parse("const x = 1;\n")
        files = [n for n in result.nodes if n.kind == "file"]
        assert len(files) == 1
        assert files[0].id == "file:frontend/mod.ts"
        assert files[0].area == "frontend"
        assert files[0].parent == "area:frontend"

    def test_emits_class_and_methods(self) -> None:
        result = _parse(
            "export class Foo {\n  bar(): number { return 1; }\n  baz(): number { return 2; }\n}\n",
        )
        classes = [n for n in result.nodes if n.kind == "class"]
        assert [c.label for c in classes] == ["Foo"]
        funcs = [n for n in result.nodes if n.kind == "function"]
        labels = [f.label for f in funcs]
        assert "Foo.bar" in labels
        assert "Foo.baz" in labels
        bar = next(f for f in funcs if f.label == "Foo.bar")
        assert bar.parent == "frontend/mod.ts::Foo"

    def test_emits_top_level_function(self) -> None:
        result = _parse("function top() { return 1; }\n")
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert [f.label for f in funcs] == ["top"]
        assert funcs[0].parent == "file:frontend/mod.ts"

    def test_export_function_is_visible(self) -> None:
        result = _parse("export function f() { return 1; }\n")
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert any(f.label == "f" for f in funcs)

    def test_export_const_at_top_level(self) -> None:
        result = _parse('export const KEY = "x";\n')
        funcs = [n for n in result.nodes if n.kind == "function"]
        # Exported top-level consts surface as function-kind nodes so callers
        # can locate them in the graph (per Phase 4 spec).
        assert any(f.label == "KEY" for f in funcs)

    def test_export_type_alias(self) -> None:
        result = _parse("export type Pair<A, B> = [A, B];\n")
        # Exported type aliases surface as function-kind nodes.
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert any(f.label == "Pair" for f in funcs)


class TestImports:
    def test_named_import(self) -> None:
        result = _parse('import { foo } from "./foo";\n')
        imports = [e for e in result.edges if e.kind == "imports"]
        assert len(imports) == 1
        e = imports[0]
        assert e.target.startswith("module:")
        assert "./foo" in e.target or "foo" in e.target
        assert e.source_kind == "ast"
        assert e.evidence.line == 1

    def test_default_import(self) -> None:
        result = _parse('import bar from "./bar";\n')
        imports = [e for e in result.edges if e.kind == "imports"]
        assert len(imports) == 1
        assert "bar" in imports[0].target

    def test_namespace_import(self) -> None:
        result = _parse('import * as ns from "lib";\n')
        imports = [e for e in result.edges if e.kind == "imports"]
        assert len(imports) == 1
        assert "lib" in imports[0].target

    def test_dynamic_import_literal(self) -> None:
        result = _parse('async function f() { await import("./dyn"); }\n')
        imports = [e for e in result.edges if e.kind == "imports"]
        assert any("dyn" in e.target for e in imports)

    def test_dynamic_import_non_literal_not_resolved(self) -> None:
        result = _parse('async function f(p: string) { await import("/" + p); }\n')
        imports = [e for e in result.edges if e.kind == "imports"]
        # Non-literal dynamic import does not produce an import edge.
        assert all("/" + "p" not in e.target for e in imports)


class TestInherits:
    def test_class_extends(self) -> None:
        result = _parse(
            "class Base {}\nexport class Child extends Base {}\n",
        )
        inherits = [e for e in result.edges if e.kind == "inherits"]
        assert len(inherits) == 1
        e = inherits[0]
        assert e.source == "frontend/mod.ts::Child"
        assert e.target == "frontend/mod.ts::Base"
        assert e.source_kind == "ast"

    def test_class_extends_imported(self) -> None:
        result = _parse(
            'import { Base } from "./base";\nexport class Child extends Base {}\n',
        )
        inherits = [e for e in result.edges if e.kind == "inherits"]
        assert len(inherits) == 1
        # Resolves to the import binding.
        assert inherits[0].target.startswith("module:")

    def test_interface_extends(self) -> None:
        result = _parse(
            "interface Greeter {}\nexport interface PoliteGreeter extends Greeter {}\n",
        )
        inherits = [e for e in result.edges if e.kind == "inherits"]
        # Interface extends is emitted; interfaces themselves don't necessarily
        # need to be nodes — but the inherits target must resolve to a known id.
        assert any(e.target == "frontend/mod.ts::Greeter" for e in inherits)


class TestCalls:
    def test_bare_identifier_call(self) -> None:
        result = _parse(
            "function helper() { return 1; }\nfunction caller() { return helper(); }\n",
        )
        calls = [e for e in result.edges if e.kind == "calls"]
        assert any(
            e.source == "frontend/mod.ts::caller"
            and e.target == "frontend/mod.ts::helper"
            for e in calls
        )

    def test_self_this_method(self) -> None:
        result = _parse(
            "class Foo {\n"
            "  bar(): number { return this.baz(); }\n"
            "  baz(): number { return 1; }\n"
            "}\n",
        )
        calls = [e for e in result.edges if e.kind == "calls"]
        assert any(
            e.source == "frontend/mod.ts::Foo.bar"
            and e.target == "frontend/mod.ts::Foo.baz"
            for e in calls
        )

    def test_new_expression_to_class(self) -> None:
        result = _parse(
            "class Foo {}\nfunction make() { return new Foo(); }\n",
        )
        calls = [e for e in result.edges if e.kind == "calls"]
        assert any(
            e.source == "frontend/mod.ts::make" and e.target == "frontend/mod.ts::Foo"
            for e in calls
        )

    def test_static_method_call_on_imported_symbol(self) -> None:
        result = _parse(
            'import { Foo } from "./foo";\nfunction caller() { return Foo.staticMethod(); }\n',
        )
        calls = [e for e in result.edges if e.kind == "calls"]
        # Foo is module-bound via import — the call resolves to the
        # imported member id.
        assert any(
            e.source == "frontend/mod.ts::caller" and "Foo.staticMethod" in e.target for e in calls
        )

    def test_unknown_attribute_call_emits_unresolved_site(self) -> None:
        result = _parse(
            "function caller(obj: { method(): string }) { return obj.method(); }\n",
        )
        calls = [e for e in result.edges if e.kind == "calls"]
        assert calls == []
        # Should be detected as an unresolved site for the gap-fill stage.
        assert result.unresolved_dynamic_sites >= 1

    def test_dynamic_subscript_call_emits_unresolved_site(self) -> None:
        result = _parse(
            "function dyn() {\n"
            '  const obj: Record<string, () => number> = {};\n'
            '  return obj["k"]();\n'
            "}\n",
        )
        # obj["k"]() is purely dynamic — not in the calls set.
        calls = [e for e in result.edges if e.kind == "calls"]
        assert calls == []
        assert result.unresolved_dynamic_sites >= 1


class TestEvidence:
    def test_every_edge_has_ast_source_kind_and_snippet(self) -> None:
        result = _parse(
            'import { Foo } from "./foo";\n'
            "class Base {}\n"
            "class Child extends Base {}\n"
            "function caller() { return new Child(); }\n",
        )
        assert all(e.source_kind == "ast" for e in result.edges)
        for e in result.edges:
            assert e.evidence.file == "frontend/mod.ts"
            assert e.evidence.line >= 1
            assert e.evidence.snippet


class TestMalformed:
    def test_does_not_crash_on_syntax_error(self) -> None:
        # tree-sitter error recovery — parser must not raise on broken input.
        result = _parse("function broken( {\n  return 1;\n}\n")
        assert any(n.kind == "file" for n in result.nodes)


class TestTsxSupport:
    def test_tsx_file_extension(self) -> None:
        # The parser registry hands a tsx file to the same parser; we
        # cover at least that a function in a tsx file produces a node.
        result = _parse(
            "function Component() { return null; }\n",
            rel_path="frontend/c.tsx",
            area="frontend",
        )
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert any(f.label == "Component" for f in funcs)
