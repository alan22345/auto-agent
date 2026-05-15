"""Tree-sitter Python parser (ADR-016 Phase 2).

Emits:

* **Nodes** — one per file / class / top-level + nested function.
* **Edges (all ``source_kind="ast"`` with real evidence)**:
    * ``imports`` — module → module from ``import`` / ``from .. import``.
    * ``inherits`` — class → parent class when the parent is bound in
      module scope (top-level def/class/import).
    * ``calls`` — function → function when the callee is statically
      resolvable in scope (direct name bound to a known function/class,
      or a same-class ``self.method`` reference).

Statically-unresolvable call sites (``getattr``, registry dicts,
attribute access on imported modules, dynamic ``__import__``, etc.) are
detected and counted in :attr:`ParseResult.unresolved_dynamic_sites`. Phase
3's LLM gap-fill will turn these into edges; Phase 2 does not.

The parser is **best-effort** under tree-sitter's error-recovery: a file
with a syntax error parses to a tree containing ``ERROR`` nodes. We do
not raise on that — the pipeline owns whether a file's failure marks the
whole area failed. Catastrophic parser errors (grammar exceptions) do
propagate; the pipeline catches them per area.

ID convention (locked across phases): ``"{file}::{symbol}"`` for classes
and functions, ``"file:{file}"`` for files, ``"area:{name}"`` for areas.
Nested function ids use ``::`` separation top-to-bottom (e.g.
``"a/b.py::OuterClass.method.inner"``). The id is also what a future
``query_repo_graph`` tool will hand callers — keep it stable.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.graph_analyzer.parsers import Parser, ParseResult
from shared.types import Edge, EdgeEvidence, Node

# ----------------------------------------------------------------------
# Lazy tree-sitter import — keep the dependency out of the import graph
# of agent modules that don't need it.
# ----------------------------------------------------------------------

_PY_LANGUAGE = None  # populated on first parse


def _get_language():
    global _PY_LANGUAGE
    if _PY_LANGUAGE is None:
        import tree_sitter_python
        from tree_sitter import Language

        _PY_LANGUAGE = Language(tree_sitter_python.language())
    return _PY_LANGUAGE


# Tree-sitter node types we read.
_FUNC_NODE = "function_definition"
_CLASS_NODE = "class_definition"
_IMPORT_NODE = "import_statement"
_IMPORT_FROM_NODE = "import_from_statement"
_CALL_NODE = "call"


@dataclass
class _Scope:
    """Names bound at file-scope (functions / classes / imports) and
    same-class methods bound inside the enclosing class body."""

    # name -> file-relative id of the resolvable function/class
    module_bindings: dict[str, str]
    # class id -> set of method names bound on that class
    class_methods: dict[str, set[str]]


class PythonParser(Parser):
    """Tree-sitter Python parser. See module docstring for behaviour."""

    extensions = (".py",)

    def parse_file(
        self,
        *,
        rel_path: str,
        area: str,
        source: bytes,
    ) -> ParseResult:
        from tree_sitter import Parser as TSParser  # local import

        ts_parser = TSParser(_get_language())
        tree = ts_parser.parse(source)

        result = ParseResult()
        file_id = f"file:{rel_path}"

        # File node — every file gets one, even if empty.
        result.nodes.append(
            Node(
                id=file_id,
                kind="file",
                label=rel_path.rsplit("/", 1)[-1],
                file=rel_path,
                line_start=1,
                line_end=tree.root_node.end_point[0] + 1,
                area=area,
                parent=f"area:{area}",
            ),
        )

        # ---- Pass 1: collect nodes + build module/class scope. ----
        scope = _Scope(module_bindings={}, class_methods={})
        self._collect_top_level(tree.root_node, rel_path, area, source, result, scope)

        # ---- Pass 2: edges (imports / inherits / calls). ----
        # Imports are emitted in pass 1 because they live at module top
        # level only. Inherits + calls walk the tree again with the full
        # scope available.
        self._collect_inherits_and_calls(
            tree.root_node,
            rel_path,
            area,
            source,
            result,
            scope,
            current_class=None,
            current_func=None,
        )

        return result

    # ------------------------------------------------------------------
    # Pass 1 — nodes and imports
    # ------------------------------------------------------------------

    def _collect_top_level(
        self,
        root,
        rel_path: str,
        area: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
    ) -> None:
        """Walk module-level children and emit class/function nodes plus
        ``imports`` edges. Nested functions / methods are discovered by
        :meth:`_collect_nested`.
        """
        file_id = f"file:{rel_path}"

        for child in root.children:
            t = child.type
            if t == _IMPORT_NODE:
                self._emit_import_edges(child, rel_path, area, source, result, scope)
            elif t == _IMPORT_FROM_NODE:
                self._emit_import_from_edges(child, rel_path, area, source, result, scope)
            elif t == _CLASS_NODE:
                cls_name = self._first_identifier(child, source)
                if cls_name is None:
                    continue
                cls_id = f"{rel_path}::{cls_name}"
                result.nodes.append(
                    Node(
                        id=cls_id,
                        kind="class",
                        label=cls_name,
                        file=rel_path,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        area=area,
                        parent=file_id,
                    ),
                )
                scope.module_bindings[cls_name] = cls_id
                scope.class_methods[cls_id] = set()
                # Walk methods inside the class body.
                body = _named_child(child, "block")
                if body is not None:
                    for member in body.children:
                        if member.type == _FUNC_NODE:
                            self._emit_function_node(
                                member,
                                rel_path,
                                area,
                                source,
                                result,
                                parent_id=cls_id,
                                qualifier=cls_name + ".",
                            )
                            fname = self._first_identifier(member, source)
                            if fname is not None:
                                scope.class_methods[cls_id].add(fname)
                                self._collect_nested(
                                    member,
                                    rel_path,
                                    area,
                                    source,
                                    result,
                                    qualifier=f"{cls_name}.{fname}.",
                                )
            elif t == _FUNC_NODE:
                self._emit_function_node(
                    child,
                    rel_path,
                    area,
                    source,
                    result,
                    parent_id=file_id,
                    qualifier="",
                )
                fname = self._first_identifier(child, source)
                if fname is not None:
                    scope.module_bindings[fname] = f"{rel_path}::{fname}"
                    self._collect_nested(
                        child,
                        rel_path,
                        area,
                        source,
                        result,
                        qualifier=f"{fname}.",
                    )

    def _collect_nested(
        self,
        func_node,
        rel_path: str,
        area: str,
        source: bytes,
        result: ParseResult,
        qualifier: str,
    ) -> None:
        """Recurse into a function/method body and emit nested function
        nodes. Nested functions inherit their parent's qualifier prefix
        (e.g. ``OuterClass.method.inner``)."""
        body = _named_child(func_node, "block")
        if body is None:
            return
        for child in body.children:
            if child.type == _FUNC_NODE:
                name = self._first_identifier(child, source)
                if name is None:
                    continue
                parent_qual = qualifier.rstrip(".")
                parent_id = f"{rel_path}::{parent_qual}"
                self._emit_function_node(
                    child,
                    rel_path,
                    area,
                    source,
                    result,
                    parent_id=parent_id,
                    qualifier=qualifier,
                )
                self._collect_nested(
                    child,
                    rel_path,
                    area,
                    source,
                    result,
                    qualifier=f"{qualifier}{name}.",
                )

    def _emit_function_node(
        self,
        node,
        rel_path: str,
        area: str,
        source: bytes,
        result: ParseResult,
        *,
        parent_id: str,
        qualifier: str,
    ) -> None:
        name = self._first_identifier(node, source)
        if name is None:
            return
        node_id = f"{rel_path}::{qualifier}{name}"
        result.nodes.append(
            Node(
                id=node_id,
                kind="function",
                label=f"{qualifier}{name}" if qualifier else name,
                file=rel_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                area=area,
                parent=parent_id,
            ),
        )

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------

    def _emit_import_edges(
        self,
        node,
        rel_path: str,
        area: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
    ) -> None:
        # ``import x`` / ``import x.y`` / ``import x as y, z as w``
        line = _line_text(source, node)
        line_no = node.start_point[0] + 1
        for child in node.named_children:
            module = None
            alias = None
            if child.type == "dotted_name":
                module = _node_text(child, source)
                alias = module.split(".")[0]
            elif child.type == "aliased_import":
                # ``x as y``
                inner = _named_child(child, "dotted_name")
                aliased = _named_child(child, "identifier")
                if inner is not None:
                    module = _node_text(inner, source)
                if aliased is not None:
                    alias = _node_text(aliased, source)
                elif module is not None:
                    alias = module.split(".")[0]
            if module is None:
                continue
            scope.module_bindings[alias or module.split(".")[0]] = f"module:{module}"
            result.edges.append(
                Edge(
                    source=f"module:{_module_from_path(rel_path)}",
                    target=f"module:{module}",
                    kind="imports",
                    evidence=EdgeEvidence(file=rel_path, line=line_no, snippet=line),
                    source_kind="ast",
                ),
            )

    def _emit_import_from_edges(
        self,
        node,
        rel_path: str,
        area: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
    ) -> None:
        line = _line_text(source, node)
        line_no = node.start_point[0] + 1

        # Module ref — either an absolute ``dotted_name`` or a
        # ``relative_import`` of form ``.[.]*<name>``.
        module_target = None
        rel_dots = 0
        rel_base = ""
        for child in node.named_children:
            if child.type == "dotted_name" and module_target is None:
                module_target = _node_text(child, source)
                break
            if child.type == "relative_import" and module_target is None:
                # Count dots, then maybe a dotted_name beneath.
                prefix_node = _named_child(child, "import_prefix")
                if prefix_node is not None:
                    rel_dots = sum(1 for c in prefix_node.children if c.type == ".")
                inner_name = _named_child(child, "dotted_name")
                rel_base = _node_text(inner_name, source) if inner_name else ""
                module_target = _resolve_relative(rel_path, rel_dots, rel_base)
                break

        if module_target is None:
            return

        # Names imported from the module — bind each to module:<target>.<name>
        # so a later ``Cat(Animal)`` resolves Animal -> the imported binding.
        for child in node.named_children[1:]:  # skip the module ref itself
            if child.type == "dotted_name":
                name = _node_text(child, source).split(".")[-1]
                scope.module_bindings[name] = f"module:{module_target}.{name}"
            elif child.type == "aliased_import":
                inner = _named_child(child, "dotted_name")
                aliased = _named_child(child, "identifier")
                if inner is not None and aliased is not None:
                    name = _node_text(inner, source).split(".")[-1]
                    scope.module_bindings[_node_text(aliased, source)] = (
                        f"module:{module_target}.{name}"
                    )

        result.edges.append(
            Edge(
                source=f"module:{_module_from_path(rel_path)}",
                target=f"module:{module_target}",
                kind="imports",
                evidence=EdgeEvidence(file=rel_path, line=line_no, snippet=line),
                source_kind="ast",
            ),
        )

    # ------------------------------------------------------------------
    # Pass 2 — inherits + calls
    # ------------------------------------------------------------------

    def _collect_inherits_and_calls(
        self,
        node,
        rel_path: str,
        area: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
        *,
        current_class: str | None,
        current_func: str | None,
    ) -> None:
        t = node.type

        if t == _CLASS_NODE:
            cls_name = self._first_identifier(node, source)
            if cls_name is not None:
                cls_id = f"{rel_path}::{cls_name}"
                # ``inherits`` — argument_list children.
                arglist = _named_child(node, "argument_list")
                line_no = node.start_point[0] + 1
                line = _line_text(source, node)
                if arglist is not None:
                    for arg in arglist.named_children:
                        if arg.type == "identifier":
                            parent_name = _node_text(arg, source)
                            target = scope.module_bindings.get(parent_name)
                            if target is not None:
                                result.edges.append(
                                    Edge(
                                        source=cls_id,
                                        target=target,
                                        kind="inherits",
                                        evidence=EdgeEvidence(
                                            file=rel_path,
                                            line=line_no,
                                            snippet=line,
                                        ),
                                        source_kind="ast",
                                    ),
                                )
                            else:
                                result.unresolved_dynamic_sites += 1
                        elif arg.type == "attribute":
                            # ``class X(mod.Base)`` — unresolved in Phase 2.
                            result.unresolved_dynamic_sites += 1
                # Recurse into class body with current_class set.
                body = _named_child(node, "block")
                if body is not None:
                    for child in body.children:
                        self._collect_inherits_and_calls(
                            child,
                            rel_path,
                            area,
                            source,
                            result,
                            scope,
                            current_class=cls_id,
                            current_func=current_func,
                        )
            return

        if t == _FUNC_NODE:
            fname = self._first_identifier(node, source)
            if fname is None:
                return
            if current_class is not None:
                func_id = f"{current_class}.{fname}"
            elif current_func is not None:
                func_id = f"{current_func}.{fname}"
            else:
                func_id = f"{rel_path}::{fname}"
            body = _named_child(node, "block")
            if body is not None:
                for child in body.children:
                    self._collect_inherits_and_calls(
                        child,
                        rel_path,
                        area,
                        source,
                        result,
                        scope,
                        current_class=current_class,
                        current_func=func_id,
                    )
            return

        if t == _CALL_NODE:
            self._handle_call(
                node,
                rel_path,
                source,
                result,
                scope,
                current_class=current_class,
                current_func=current_func,
            )
            # Calls can contain nested calls in their arguments — descend.
            for child in node.children:
                self._collect_inherits_and_calls(
                    child,
                    rel_path,
                    area,
                    source,
                    result,
                    scope,
                    current_class=current_class,
                    current_func=current_func,
                )
            return

        # Default — recurse.
        for child in node.children:
            self._collect_inherits_and_calls(
                child,
                rel_path,
                area,
                source,
                result,
                scope,
                current_class=current_class,
                current_func=current_func,
            )

    def _handle_call(
        self,
        node,
        rel_path: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
        *,
        current_class: str | None,
        current_func: str | None,
    ) -> None:
        # A call's first child is its callee (identifier / attribute / etc.).
        callee = node.children[0] if node.children else None
        if callee is None:
            return
        line_no = node.start_point[0] + 1
        line = _line_text(source, node)

        # ``foo(...)`` — identifier in module scope.
        if callee.type == "identifier":
            name = _node_text(callee, source)
            target = scope.module_bindings.get(name)
            if target is not None and current_func is not None:
                result.edges.append(
                    Edge(
                        source=current_func,
                        target=target,
                        kind="calls",
                        evidence=EdgeEvidence(file=rel_path, line=line_no, snippet=line),
                        source_kind="ast",
                    ),
                )
                return
            # Unbound identifier — count as dynamic only if inside a function.
            if current_func is not None:
                result.unresolved_dynamic_sites += 1
            return

        # ``self.method(...)`` — resolved when inside the class that defines
        # ``method``.
        if callee.type == "attribute" and current_class is not None and current_func is not None:
            obj, attr = _split_attribute(callee, source)
            if obj == "self" and attr is not None:
                methods = scope.class_methods.get(current_class, set())
                if attr in methods:
                    result.edges.append(
                        Edge(
                            source=current_func,
                            target=f"{current_class}.{attr}",
                            kind="calls",
                            evidence=EdgeEvidence(
                                file=rel_path,
                                line=line_no,
                                snippet=line,
                            ),
                            source_kind="ast",
                        ),
                    )
                    return
                # ``self.unknown_method`` — unresolved (may be inherited).
                result.unresolved_dynamic_sites += 1
                return
            # ``mod.something()`` or ``obj.method()`` — unresolved in Phase 2.
            result.unresolved_dynamic_sites += 1
            return

        # ``obj.method()`` outside a class — unresolved.
        if current_func is not None:
            result.unresolved_dynamic_sites += 1

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _first_identifier(node, source: bytes) -> str | None:
        for c in node.children:
            if c.type == "identifier":
                return _node_text(c, source)
        return None


# ----------------------------------------------------------------------
# Module-level utility helpers (kept out of the class so they can be
# trivially reused if a TS parser lands and needs similar handling).
# ----------------------------------------------------------------------


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _line_text(source: bytes, node) -> str:
    """The (single) source line of ``node``'s start position, stripped.

    Tree-sitter exposes start_point/end_point as (row, col). We slice the
    raw source bytes for the row to produce a short, readable evidence
    snippet. Multi-line statements still report only the start line —
    matches what a human grepping the codebase sees first.
    """
    row = node.start_point[0]
    lines = source.split(b"\n")
    if row >= len(lines):
        return ""
    return lines[row].decode("utf-8", errors="replace").strip()


def _named_child(node, type_name: str):
    """Return the first direct child of ``node`` whose ``type`` matches."""
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _split_attribute(node, source: bytes) -> tuple[str | None, str | None]:
    """Split an ``a.b`` attribute access into its parts.

    Tree-sitter exposes the receiver and the attribute as the first and
    last named children of the ``attribute`` node; everything in between
    is punctuation.
    """
    named = [c for c in node.children if c.type in ("identifier", "attribute")]
    if not named:
        return (None, None)
    obj = _node_text(named[0], source) if named[0].type == "identifier" else None
    attr_node = None
    for c in node.children:
        if c.type == "identifier":
            attr_node = c
    attr = _node_text(attr_node, source) if attr_node is not None else None
    return (obj, attr)


def _module_from_path(rel_path: str) -> str:
    """Convert ``a/b/c.py`` → ``a.b.c``; ``__init__.py`` collapses to its
    package."""
    no_ext = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    parts = no_ext.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(rel_path: str, dots: int, name: str) -> str:
    """Resolve a relative import (``from ..x import y``) to its absolute
    module path, given the importing file's relative path.

    ``rel_path`` is workspace-relative (``a/b/c.py``). ``dots`` counts the
    leading ``.``s in the import. With 1 dot we stay in the package;
    each extra dot pops one package level.
    """
    parts = rel_path.split("/")
    # File belongs to its parent package.
    pkg_parts = parts[:-1]
    pops = max(dots - 1, 0)
    if pops:
        pkg_parts = pkg_parts[:-pops] if pops <= len(pkg_parts) else []
    target = ".".join(pkg_parts)
    if name:
        target = f"{target}.{name}" if target else name
    return target
