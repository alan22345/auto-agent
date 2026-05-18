"""Tree-sitter TypeScript parser (ADR-016 Phase 4 §typescript).

Emits the same :class:`ParseResult` shape as the Python parser — the
pipeline never branches on language. Adding TypeScript is therefore a
single new file plus one extension registry entry in ``parsers/__init__.py``.

Emits:

* **Nodes** — one per file / class / function-or-method. Per Phase 4
  spec, ``export const`` and ``export type`` at module scope also surface
  as function-kind nodes so consumers can locate them.
* **Edges (all ``source_kind="ast"`` with real evidence)**:
    * ``imports`` — from named, default, namespace, and dynamic-literal
      ``import("...")`` forms. The import target is ``module:<spec>``
      (the source string verbatim — no path-resolution beyond stripping
      the file extension if present).
    * ``inherits`` — ``class X extends Y`` and ``interface X extends Y``
      when ``Y`` is bound in module scope.
    * ``calls`` — bare identifier in module scope, ``this.method()``
      within a class that defines ``method``, ``new Foo()`` collapsed to
      ``calls -> Foo`` (constructor), and ``Foo.method()`` when ``Foo``
      is a module-bound imported symbol.

Anything else (dynamic dispatch, ``obj.method()`` outside a known shape,
template-string URLs, subscript calls) lands as an :class:`UnresolvedSite`
for the existing gap-fill / agent-escape machinery to attempt.

Tree-sitter for TypeScript is intentionally weaker than ``ts-morph``
(which has full TS-compiler type info); ADR-016 §5 accepts this and
flags it as a future swap point.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.graph_analyzer.parsers import Parser, ParseResult
from agent.graph_analyzer.types import PatternHint, UnresolvedSite
from shared.types import Edge, EdgeEvidence, Node

# Surrounding window for unresolved sites — same as the Python parser.
_SURROUNDING_LINES_BEFORE = 15
_SURROUNDING_LINES_AFTER = 15


# ----------------------------------------------------------------------
# Lazy tree-sitter import — the dependency only loads when a TS file is
# encountered, so test suites that never touch TS pay no cost.
# ----------------------------------------------------------------------

_TS_LANGUAGE = None
_TSX_LANGUAGE = None


def _get_language(rel_path: str):
    global _TS_LANGUAGE, _TSX_LANGUAGE
    import tree_sitter_typescript
    from tree_sitter import Language

    if rel_path.endswith(".tsx"):
        if _TSX_LANGUAGE is None:
            _TSX_LANGUAGE = Language(tree_sitter_typescript.language_tsx())
        return _TSX_LANGUAGE
    if _TS_LANGUAGE is None:
        _TS_LANGUAGE = Language(tree_sitter_typescript.language_typescript())
    return _TS_LANGUAGE


# Tree-sitter node types we read.
_FUNC_NODE = "function_declaration"
_CLASS_NODE = "class_declaration"
_INTERFACE_NODE = "interface_declaration"
_METHOD_NODE = "method_definition"
_IMPORT_NODE = "import_statement"
_EXPORT_NODE = "export_statement"
_LEXICAL_DECLARATION = "lexical_declaration"
_VARIABLE_DECLARATOR = "variable_declarator"
_TYPE_ALIAS_NODE = "type_alias_declaration"
_CALL_NODE = "call_expression"
_NEW_NODE = "new_expression"


@dataclass
class _Scope:
    """Names bound at file-scope (functions / classes / imports) and
    same-class methods bound inside the enclosing class body."""

    # name -> file-relative id of the resolvable function/class/etc.
    module_bindings: dict[str, str]
    # class id -> set of method names bound on that class
    class_methods: dict[str, set[str]]


class TypeScriptParser(Parser):
    """Tree-sitter TypeScript / TSX parser. See module docstring."""

    extensions = (".ts", ".tsx")

    def parse_file(
        self,
        *,
        rel_path: str,
        area: str,
        source: bytes,
    ) -> ParseResult:
        from tree_sitter import Parser as TSParser  # local import

        ts_parser = TSParser(_get_language(rel_path))
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

        scope = _Scope(module_bindings={}, class_methods={})

        # Pass 1 — top-level walk for nodes + imports.
        self._collect_top_level(tree.root_node, rel_path, area, source, result, scope)

        # Pass 2 — inherits + calls + unresolved sites.
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

        # Pass 3 (Phase 5) — public-surface inference. Skipped for files
        # whose path segments mark them as implicitly private; otherwise
        # walks the top level once more pulling names out of the
        # ``export``-wrapped declarations.
        if not _file_is_implicitly_private(rel_path):
            result.public_symbols = _compute_public_symbols(
                root=tree.root_node,
                source=source,
                rel_path=rel_path,
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
        file_id = f"file:{rel_path}"
        for child in root.children:
            self._dispatch_top_level(
                child,
                rel_path,
                area,
                source,
                result,
                scope,
                parent_id=file_id,
            )

    def _dispatch_top_level(
        self,
        child,
        rel_path: str,
        area: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
        *,
        parent_id: str,
    ) -> None:
        t = child.type

        if t == _IMPORT_NODE:
            self._emit_import(child, rel_path, source, result, scope)
            return

        if t == _EXPORT_NODE:
            # An ``export`` statement wraps the actual declaration —
            # recurse into its children. The declaration is named
            # ``declaration`` field but tree-sitter exposes it as a
            # direct child whose type is one of the declaration kinds.
            for grand in child.children:
                if grand.type in (
                    _FUNC_NODE,
                    _CLASS_NODE,
                    _INTERFACE_NODE,
                    _LEXICAL_DECLARATION,
                    _TYPE_ALIAS_NODE,
                ):
                    self._dispatch_top_level(
                        grand,
                        rel_path,
                        area,
                        source,
                        result,
                        scope,
                        parent_id=parent_id,
                    )
            return

        if t == _CLASS_NODE:
            self._emit_class_node(child, rel_path, area, source, result, scope, parent_id)
            return

        if t == _INTERFACE_NODE:
            # Interfaces become module-bound names too (for inherits
            # resolution) but we don't emit a class node for them —
            # they have no runtime presence. Bind the name only.
            name = _type_identifier(child, source)
            if name is not None:
                scope.module_bindings[name] = f"{rel_path}::{name}"
            return

        if t == _FUNC_NODE:
            self._emit_function_node(
                child,
                rel_path,
                area,
                source,
                result,
                parent_id=parent_id,
                qualifier="",
            )
            fname = _identifier_of(child, source)
            if fname is not None:
                scope.module_bindings[fname] = f"{rel_path}::{fname}"
            return

        if t == _LEXICAL_DECLARATION:
            # ``export const X = ...`` (or ``const X = ...``). Surface
            # the named variables as function-kind nodes if they appear
            # at the top level and the value looks like a binding the
            # graph should know about. Phase 4 keeps this simple: every
            # top-level lexical declarator becomes a node so HTTP
            # matching can refer to it.
            for declr in child.children:
                if declr.type == _VARIABLE_DECLARATOR:
                    self._emit_const_node(
                        declr,
                        rel_path,
                        area,
                        source,
                        result,
                        scope,
                        parent_id=parent_id,
                    )
            return

        if t == _TYPE_ALIAS_NODE:
            name = _type_identifier(child, source)
            if name is None:
                return
            node_id = f"{rel_path}::{name}"
            result.nodes.append(
                Node(
                    id=node_id,
                    kind="function",
                    label=name,
                    file=rel_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    area=area,
                    parent=parent_id,
                ),
            )
            scope.module_bindings[name] = node_id
            return

    def _emit_class_node(
        self,
        node,
        rel_path: str,
        area: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
        parent_id: str,
    ) -> None:
        cls_name = _type_identifier(node, source)
        if cls_name is None:
            return
        cls_id = f"{rel_path}::{cls_name}"
        result.nodes.append(
            Node(
                id=cls_id,
                kind="class",
                label=cls_name,
                file=rel_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                area=area,
                parent=parent_id,
            ),
        )
        scope.module_bindings[cls_name] = cls_id
        scope.class_methods[cls_id] = set()

        body = _named_child(node, "class_body")
        if body is None:
            return
        for member in body.children:
            if member.type == _METHOD_NODE:
                mname = _property_identifier(member, source)
                if mname is None:
                    continue
                func_id = f"{cls_id}.{mname}"
                result.nodes.append(
                    Node(
                        id=func_id,
                        kind="function",
                        label=f"{cls_name}.{mname}",
                        file=rel_path,
                        line_start=member.start_point[0] + 1,
                        line_end=member.end_point[0] + 1,
                        area=area,
                        parent=cls_id,
                    ),
                )
                scope.class_methods[cls_id].add(mname)

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
        name = _identifier_of(node, source)
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

    def _emit_const_node(
        self,
        declr,
        rel_path: str,
        area: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
        *,
        parent_id: str,
    ) -> None:
        name_node = None
        for c in declr.children:
            if c.type == "identifier":
                name_node = c
                break
        if name_node is None:
            return
        name = _node_text(name_node, source)
        node_id = f"{rel_path}::{name}"
        result.nodes.append(
            Node(
                id=node_id,
                kind="function",
                label=name,
                file=rel_path,
                line_start=declr.start_point[0] + 1,
                line_end=declr.end_point[0] + 1,
                area=area,
                parent=parent_id,
            ),
        )
        scope.module_bindings[name] = node_id

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------

    def _emit_import(
        self,
        node,
        rel_path: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
    ) -> None:
        spec = _import_specifier_string(node, source)
        if spec is None:
            return
        target_id = f"module:{spec}"
        line_no = node.start_point[0] + 1
        line = _line_text(source, node)

        # Record every named/default/namespace identifier as a module
        # binding so subsequent ``Foo.method()`` or ``new Foo()`` resolve.
        clause = _named_child(node, "import_clause")
        if clause is not None:
            for c in clause.children:
                if c.type == "identifier":
                    # default import: ``import Foo from "..."``
                    scope.module_bindings[_node_text(c, source)] = (
                        f"{target_id}.{_node_text(c, source)}"
                    )
                elif c.type == "namespace_import":
                    # ``import * as ns from "..."``
                    for nn in c.children:
                        if nn.type == "identifier":
                            scope.module_bindings[_node_text(nn, source)] = target_id
                elif c.type == "named_imports":
                    for spec_node in c.children:
                        if spec_node.type == "import_specifier":
                            ident = _identifier_of(spec_node, source)
                            if ident is not None:
                                scope.module_bindings[ident] = f"{target_id}.{ident}"

        result.edges.append(
            Edge(
                source=f"module:{_module_from_path(rel_path)}",
                target=target_id,
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
            cls_name = _type_identifier(node, source)
            cls_id = f"{rel_path}::{cls_name}" if cls_name else None
            if cls_id is not None:
                # ``class X extends Y``
                heritage = _named_child(node, "class_heritage")
                if heritage is not None:
                    for h in heritage.children:
                        if h.type == "extends_clause":
                            for ec in h.children:
                                if ec.type == "identifier":
                                    parent_name = _node_text(ec, source)
                                    target = scope.module_bindings.get(parent_name)
                                    if target is not None:
                                        result.edges.append(
                                            Edge(
                                                source=cls_id,
                                                target=target,
                                                kind="inherits",
                                                evidence=EdgeEvidence(
                                                    file=rel_path,
                                                    line=node.start_point[0] + 1,
                                                    snippet=_line_text(source, node),
                                                ),
                                                source_kind="ast",
                                            ),
                                        )
                # Recurse into class body with current_class set.
                body = _named_child(node, "class_body")
                if body is not None:
                    for member in body.children:
                        self._collect_inherits_and_calls(
                            member,
                            rel_path,
                            area,
                            source,
                            result,
                            scope,
                            current_class=cls_id,
                            current_func=current_func,
                        )
            return

        if t == _INTERFACE_NODE:
            iface_name = _type_identifier(node, source)
            if iface_name is not None:
                iface_id = f"{rel_path}::{iface_name}"
                ext = _named_child(node, "extends_type_clause")
                if ext is not None:
                    for ec in ext.children:
                        if ec.type == "type_identifier":
                            parent_name = _node_text(ec, source)
                            target = scope.module_bindings.get(
                                parent_name,
                                f"{rel_path}::{parent_name}",
                            )
                            result.edges.append(
                                Edge(
                                    source=iface_id,
                                    target=target,
                                    kind="inherits",
                                    evidence=EdgeEvidence(
                                        file=rel_path,
                                        line=node.start_point[0] + 1,
                                        snippet=_line_text(source, node),
                                    ),
                                    source_kind="ast",
                                ),
                            )
            return

        if t == _METHOD_NODE and current_class is not None:
            mname = _property_identifier(node, source)
            if mname is None:
                return
            func_id = f"{current_class}.{mname}"
            body = _named_child(node, "statement_block")
            if body is not None:
                self._walk_body(
                    body,
                    rel_path,
                    source,
                    result,
                    scope,
                    current_class=current_class,
                    current_func=func_id,
                )
            return

        if t == _FUNC_NODE:
            fname = _identifier_of(node, source)
            if fname is None:
                return
            func_id = f"{rel_path}::{fname}"
            body = _named_child(node, "statement_block")
            if body is not None:
                self._walk_body(
                    body,
                    rel_path,
                    source,
                    result,
                    scope,
                    current_class=current_class,
                    current_func=func_id,
                )
            return

        # Otherwise, descend so we discover classes / functions nested
        # at file scope under export wrappers etc.
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

    def _walk_body(
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
        """Walk into a function/method body and handle calls / new
        expressions / inline class+function declarations."""
        if node.type == _CALL_NODE:
            self._handle_call(
                node,
                rel_path,
                source,
                result,
                scope,
                current_class=current_class,
                current_func=current_func,
            )
            # Descend into args for nested calls.
            for child in node.children:
                self._walk_body(
                    child,
                    rel_path,
                    source,
                    result,
                    scope,
                    current_class=current_class,
                    current_func=current_func,
                )
            return

        if node.type == _NEW_NODE:
            self._handle_new(
                node,
                rel_path,
                source,
                result,
                scope,
                current_func=current_func,
            )
            for child in node.children:
                self._walk_body(
                    child,
                    rel_path,
                    source,
                    result,
                    scope,
                    current_class=current_class,
                    current_func=current_func,
                )
            return

        for child in node.children:
            self._walk_body(
                child,
                rel_path,
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
        callee = node.children[0] if node.children else None
        if callee is None or current_func is None:
            return
        line_no = node.start_point[0] + 1
        line = _line_text(source, node)

        # Dynamic ``import("...")`` — handled here (it's a call expression
        # in TS, not a statement). Emit an imports edge when the argument
        # is a literal string.
        if callee.type == "import":
            args = _named_child(node, "arguments")
            if args is not None:
                lit = _string_literal(args, source)
                if lit is not None:
                    result.edges.append(
                        Edge(
                            source=f"module:{_module_from_path(rel_path)}",
                            target=f"module:{lit}",
                            kind="imports",
                            evidence=EdgeEvidence(
                                file=rel_path,
                                line=line_no,
                                snippet=line,
                            ),
                            source_kind="ast",
                        ),
                    )
            # No call-edge for dynamic import.
            return

        # ``foo(...)`` — identifier in scope.
        if callee.type == "identifier":
            name = _node_text(callee, source)
            target = scope.module_bindings.get(name)
            if target is not None:
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
            _record_unresolved_site(
                result,
                rel_path=rel_path,
                line_no=line_no,
                snippet=line,
                containing_node_id=current_func,
                source=source,
                pattern_hint="unknown",
            )
            return

        # ``this.method(...)``
        if callee.type == "member_expression":
            obj, prop = _split_member(callee, source)
            if obj == "this" and prop is not None and current_class is not None:
                methods = scope.class_methods.get(current_class, set())
                if prop in methods:
                    result.edges.append(
                        Edge(
                            source=current_func,
                            target=f"{current_class}.{prop}",
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
                _record_unresolved_site(
                    result,
                    rel_path=rel_path,
                    line_no=line_no,
                    snippet=line,
                    containing_node_id=current_func,
                    source=source,
                    pattern_hint="unknown",
                )
                return
            # ``Foo.method(...)`` — receiver is a module-bound name.
            if obj is not None and prop is not None:
                receiver_target = scope.module_bindings.get(obj)
                if receiver_target is not None:
                    # Build a target id by appending the member access.
                    if receiver_target.startswith("module:"):
                        # ``module:./foo.Foo`` -> ``module:./foo.Foo.method``
                        derived = f"{receiver_target}.{prop}"
                    else:
                        derived = f"{receiver_target}.{prop}"
                    result.edges.append(
                        Edge(
                            source=current_func,
                            target=derived,
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
            # ``obj.method(...)`` with unknown receiver.
            _record_unresolved_site(
                result,
                rel_path=rel_path,
                line_no=line_no,
                snippet=line,
                containing_node_id=current_func,
                source=source,
                pattern_hint="dict_call",
            )
            return

        # Subscript or other dynamic shapes.
        _record_unresolved_site(
            result,
            rel_path=rel_path,
            line_no=line_no,
            snippet=line,
            containing_node_id=current_func,
            source=source,
            pattern_hint=_classify_call_pattern(callee),
        )

    def _handle_new(
        self,
        node,
        rel_path: str,
        source: bytes,
        result: ParseResult,
        scope: _Scope,
        *,
        current_func: str | None,
    ) -> None:
        if current_func is None:
            return
        # tree-sitter's ``new_expression`` first non-keyword child is the
        # callee.
        callee = None
        for c in node.children:
            if c.type in ("identifier", "member_expression"):
                callee = c
                break
        if callee is None:
            return
        line_no = node.start_point[0] + 1
        line = _line_text(source, node)
        if callee.type == "identifier":
            name = _node_text(callee, source)
            target = scope.module_bindings.get(name)
            if target is not None:
                result.edges.append(
                    Edge(
                        source=current_func,
                        target=target,
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
        _record_unresolved_site(
            result,
            rel_path=rel_path,
            line_no=line_no,
            snippet=line,
            containing_node_id=current_func,
            source=source,
            pattern_hint="unknown",
        )


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _record_unresolved_site(
    result: ParseResult,
    *,
    rel_path: str,
    line_no: int,
    snippet: str,
    containing_node_id: str,
    source: bytes,
    pattern_hint: PatternHint,
) -> None:
    surrounding = _surrounding_lines(
        source,
        line_no,
        before=_SURROUNDING_LINES_BEFORE,
        after=_SURROUNDING_LINES_AFTER,
    )
    result.unresolved_sites.append(
        UnresolvedSite(
            file=rel_path,
            line=line_no,
            snippet=snippet,
            containing_node_id=containing_node_id,
            surrounding_code=surrounding,
            pattern_hint=pattern_hint,
        ),
    )


def _surrounding_lines(source: bytes, line_no: int, *, before: int, after: int) -> str:
    if line_no < 1:
        line_no = 1
    lines = source.split(b"\n")
    start = max(0, line_no - 1 - before)
    end = min(len(lines), line_no - 1 + after + 1)
    return b"\n".join(lines[start:end]).decode("utf-8", errors="replace")


def _classify_call_pattern(callee) -> PatternHint:
    if callee.type == "subscript_expression":
        return "registry"
    if callee.type == "member_expression":
        return "dict_call"
    return "unknown"


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _line_text(source: bytes, node) -> str:
    row = node.start_point[0]
    lines = source.split(b"\n")
    if row >= len(lines):
        return ""
    return lines[row].decode("utf-8", errors="replace").strip()


def _named_child(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _identifier_of(node, source: bytes) -> str | None:
    """Return the first direct ``identifier`` child of ``node``."""
    for c in node.children:
        if c.type == "identifier":
            return _node_text(c, source)
    return None


def _property_identifier(node, source: bytes) -> str | None:
    for c in node.children:
        if c.type == "property_identifier":
            return _node_text(c, source)
    return None


def _type_identifier(node, source: bytes) -> str | None:
    for c in node.children:
        if c.type == "type_identifier":
            return _node_text(c, source)
    return None


def _import_specifier_string(node, source: bytes) -> str | None:
    """Pull the source-string from an ``import_statement`` and strip
    quotes plus a trailing ``.ts``/``.tsx``/``.js`` extension. The
    Phase 4 spec does not require full module resolution — the literal
    spec (e.g. ``./foo``, ``react``) is enough for the graph."""
    for c in node.children:
        if c.type == "string":
            for inner in c.children:
                if inner.type == "string_fragment":
                    text = _node_text(inner, source)
                    return _strip_ts_ext(text)
    return None


def _string_literal(args_node, source: bytes) -> str | None:
    """Extract the literal string from an ``arguments`` node, if the
    first argument is a string literal with a static fragment. Returns
    ``None`` for template-literal-with-substitutions and non-string
    arguments."""
    for c in args_node.children:
        if c.type == "string":
            for inner in c.children:
                if inner.type == "string_fragment":
                    return _strip_ts_ext(_node_text(inner, source))
            return None
        if c.type == "template_string":
            # Template with no substitutions: a single ``string_fragment``
            # child and no ``${...}`` parts.
            has_subst = any(cc.type == "template_substitution" for cc in c.children)
            if has_subst:
                return None
            for inner in c.children:
                if inner.type == "string_fragment":
                    return _node_text(inner, source)
            return None
    return None


def _split_member(node, source: bytes) -> tuple[str | None, str | None]:
    """Split ``a.b`` into ``("a", "b")``. ``a`` is left ``None`` when
    the receiver is not a bare identifier (chained access etc.)."""
    obj_name: str | None = None
    prop_name: str | None = None
    for c in node.children:
        if c.type == "identifier" and obj_name is None:
            obj_name = _node_text(c, source)
        elif c.type == "this" and obj_name is None:
            obj_name = "this"
        elif c.type == "property_identifier":
            prop_name = _node_text(c, source)
    return (obj_name, prop_name)


def _module_from_path(rel_path: str) -> str:
    """Convert ``frontend/mod.ts`` → ``frontend.mod``. ``.tsx`` likewise."""
    no_ext = _strip_ts_ext(rel_path)
    return no_ext.replace("/", ".")


def _strip_ts_ext(path: str) -> str:
    for ext in (".tsx", ".ts", ".jsx", ".js"):
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


# ----------------------------------------------------------------------
# Public-surface inference (ADR-016 Phase 5 §7)
# ----------------------------------------------------------------------


def _file_is_implicitly_private(rel_path: str) -> bool:
    """True iff ``rel_path`` is structurally private to its own file.

    Path-segment rules: any segment named ``internal`` or ``private``
    marks the file private. Filename rules: a basename starting with
    ``_`` marks the file private. The check operates on the
    forward-slash-normalised workspace-relative path.
    """
    parts = rel_path.split("/")
    for seg in parts[:-1]:
        if seg in ("internal", "private"):
            return True
    basename = parts[-1] if parts else rel_path
    return basename.startswith("_")


def _compute_public_symbols(
    *,
    root,
    source: bytes,
    rel_path: str,
) -> set[str]:
    """Walk the module top level and collect node ids of ``export``-ed
    declarations.

    Recognised declaration kinds inside an ``export_statement``:
      * ``function_declaration`` — ``export function foo()``
      * ``class_declaration`` — ``export class Foo``
      * ``interface_declaration`` — ``export interface Foo``
      * ``type_alias_declaration`` — ``export type Foo = ...``
      * ``lexical_declaration`` — ``export const FOO = ...`` (any number
        of variable_declarator children).

    Non-exported declarations and re-exports (``export { foo }``,
    ``export * from "..."``) are ignored — the v1 spec covers value
    declarations.
    """
    out: set[str] = set()
    for child in root.children:
        if child.type != _EXPORT_NODE:
            continue
        for inner in child.children:
            if inner.type in (_FUNC_NODE, _CLASS_NODE):
                name = _identifier_of(inner, source) or _type_identifier(inner, source)
                if name is not None:
                    out.add(f"{rel_path}::{name}")
            elif inner.type in (_INTERFACE_NODE, _TYPE_ALIAS_NODE):
                name = _type_identifier(inner, source)
                if name is not None:
                    out.add(f"{rel_path}::{name}")
            elif inner.type == _LEXICAL_DECLARATION:
                for declr in inner.children:
                    if declr.type != _VARIABLE_DECLARATOR:
                        continue
                    for cc in declr.children:
                        if cc.type == "identifier":
                            name = _node_text(cc, source)
                            out.add(f"{rel_path}::{name}")
                            break
    return out


__all__ = ["TypeScriptParser"]
