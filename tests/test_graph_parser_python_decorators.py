"""Python parser decorator handling (ADR-016 Phase 4).

Tree-sitter wraps decorated defs in a ``decorated_definition`` node. The
Phase 2 parser walked module-level children expecting bare
``function_definition`` / ``class_definition`` nodes and therefore *skipped*
every decorated def — the FastAPI route handlers, Click commands, dataclasses,
and pytest fixtures of the world. Phase 4 fixes that by descending into the
wrapper and capturing the raw decorator source on the wrapped node.

These tests pin:

* Decorated functions emit a node with a non-empty ``decorators`` list and a
  ``line_start`` that matches the ``def`` line (not the decorator line).
* Decorated classes are treated the same.
* Multiple decorators stack in source order.
* Async-decorated and decorator-stack-on-class both work.
* Calls inside a decorated function still resolve normally — the decorator
  wrapping does not break call discovery.
* Undecorated defs still have ``decorators == []``.
"""

from __future__ import annotations

from agent.graph_analyzer.parsers.python import PythonParser


def _parse(text: str, *, rel_path: str = "pkg/mod.py", area: str = "pkg"):
    return PythonParser().parse_file(
        rel_path=rel_path,
        area=area,
        source=text.encode(),
    )


class TestDecoratedFunctions:
    def test_simple_decorator_captured(self) -> None:
        result = _parse(
            '@router.get("/api/repos")\ndef list_repos():\n    return []\n',
        )
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert [f.label for f in funcs] == ["list_repos"]
        node = funcs[0]
        # decorator source captured verbatim with leading @.
        assert node.decorators == ['@router.get("/api/repos")']
        # line_start is the def line (line 2), not the decorator line.
        assert node.line_start == 2

    def test_async_decorated_function(self) -> None:
        result = _parse(
            '@app.post("/api/items")\nasync def add_item(x):\n    return x\n',
        )
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].decorators == ['@app.post("/api/items")']
        assert funcs[0].line_start == 2

    def test_multiple_decorators_in_source_order(self) -> None:
        result = _parse(
            '@click.command()\n@click.option("--n")\ndef cli(n):\n    return n\n',
        )
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].decorators == ["@click.command()", '@click.option("--n")']
        assert funcs[0].line_start == 3

    def test_undecorated_function_has_empty_decorators(self) -> None:
        result = _parse("def plain():\n    return 1\n")
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert funcs[0].decorators == []

    def test_calls_inside_decorated_function_still_resolved(self) -> None:
        """Wrapping by ``decorated_definition`` must not hide the body —
        statically-resolvable calls still produce edges."""
        result = _parse(
            "def helper():\n    return 1\n\n@router.get('/x')\ndef view():\n    return helper()\n",
        )
        calls = [e for e in result.edges if e.kind == "calls"]
        assert any(
            e.source == "pkg/mod.py::view" and e.target == "pkg/mod.py::helper" for e in calls
        )


class TestDecoratedClasses:
    def test_decorated_class_captured(self) -> None:
        result = _parse("@dataclass\nclass Foo:\n    x: int = 0\n")
        classes = [n for n in result.nodes if n.kind == "class"]
        assert [c.label for c in classes] == ["Foo"]
        assert classes[0].decorators == ["@dataclass"]
        assert classes[0].line_start == 2

    def test_decorated_class_methods_visible(self) -> None:
        result = _parse(
            "@dataclass\nclass Foo:\n    def bar(self):\n        return 1\n",
        )
        funcs = [n for n in result.nodes if n.kind == "function"]
        labels = [f.label for f in funcs]
        assert "Foo.bar" in labels
        # The method itself isn't decorated, so its decorators list is empty.
        method = next(f for f in funcs if f.label == "Foo.bar")
        assert method.decorators == []

    def test_router_method_decorator_inside_class(self) -> None:
        """Methods inside a class can themselves be decorated — Phase 4
        must capture those for HTTP discovery on class-based routers."""
        result = _parse(
            "class Routes:\n    @router.get('/api/x')\n    def list_x(self):\n        return []\n",
        )
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert [f.label for f in funcs] == ["Routes.list_x"]
        assert funcs[0].decorators == ["@router.get('/api/x')"]
