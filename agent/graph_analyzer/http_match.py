"""Cross-language HTTP matching (ADR-016 Phase 4 §http_match).

Links TypeScript frontend ``fetch`` / ``axios`` calls to Python backend
route handlers by URL pattern. Three stages:

1. **Backend route discovery** — walk function-kind nodes that carry a
   route-decorator on their :attr:`shared.types.Node.decorators` field
   (FastAPI ``@router.get/post/...``, FastAPI ``@router.api_route``,
   Flask ``@router.route(methods=[...])``). Other decorators are
   ignored. Multiple route decorators on one function emit multiple
   :class:`BackendRoute`s.

2. **Frontend HTTP-call discovery** — parse each ``.ts`` / ``.tsx`` file
   in the workspace with tree-sitter and find ``fetch("...")``,
   ``axios.{get|post|put|patch|delete}("...")``, and
   ``axios("url", {method: "..."})`` calls whose URL argument is a plain
   string literal. Template-literal URLs with ``${...}`` substitutions
   are skipped — they would require tracking variable values and are
   out of scope for Phase 4. Containing-node lookup uses the node line
   ranges already produced by the per-area parser pass.

3. **Matching** — for each frontend call:
   * collect candidate routes where the HTTP method matches (case-
     insensitive) and the path pattern matches with ``{param}`` and
     ``:param`` segments treated as ``*`` wildcards;
   * **0 candidates** → no edge, no error;
   * **1 candidate** → :class:`shared.types.Edge` with
     ``source_kind="ast"`` (no LLM call);
   * **≥2 candidates** → one focused LLM call (``complete_json``) asking
     which route id to pick. The reply is validated against the
     candidate pool and the existing
     :func:`agent.graph_analyzer.validator.validate_citation` against
     the frontend call line.

Failure isolation:
  * tree-sitter parsing exception per TS file → log + drop that file;
  * LLM exception during disambiguation → log + drop that match.

Phase 5 will own the ``boundary_violation`` flag for cross-area edges
in general. HTTP edges land with ``boundary_violation=False`` here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field, ValidationError

from agent.graph_analyzer.validator import validate_citation, validate_target
from agent.llm.structured import complete_json
from agent.llm.types import Message
from shared.types import Edge, EdgeEvidence

if TYPE_CHECKING:
    from agent.llm.base import LLMProvider
    from shared.types import Node

log = structlog.get_logger(__name__)

_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

#: Match ``@<receiver>.<verb>("<path>")`` — used for ``@router.get(...)``,
#: ``@app.post(...)``, etc. ``<receiver>`` and ``<verb>`` are captured.
_DECO_VERB_RE = re.compile(
    r"@(?P<receiver>[A-Za-z_][A-Za-z_0-9]*)\.(?P<verb>"
    + "|".join(m.lower() for m in _HTTP_METHODS)
    + r')\(\s*[\'"](?P<path>[^\'"\)]+)[\'"]',
)

#: Match ``@router.api_route("/x", methods=["GET", ...])`` — FastAPI's
#: generic form. ``<receiver>`` and ``<path>`` plus a methods list are
#: captured.
_DECO_API_ROUTE_RE = re.compile(
    r"@(?P<receiver>[A-Za-z_][A-Za-z_0-9]*)\.api_route\(\s*"
    r'[\'"](?P<path>[^\'"\)]+)[\'"]'
    r".*?methods\s*=\s*\[(?P<methods>[^\]]*)\]",
    flags=re.DOTALL,
)

#: Match ``@app.route("/x", methods=["GET", ...])`` — Flask's form.
_DECO_FLASK_ROUTE_RE = re.compile(
    r"@(?P<receiver>[A-Za-z_][A-Za-z_0-9]*)\.route\(\s*"
    r'[\'"](?P<path>[^\'"\)]+)[\'"]'
    r".*?methods\s*=\s*\[(?P<methods>[^\]]*)\]",
    flags=re.DOTALL,
)

#: Extract individual quoted method strings from a ``methods=[...]``
#: list.
_METHOD_TOKEN_RE = re.compile(r'[\'"]([A-Za-z_]+)[\'"]')

#: Path template segment matchers — both FastAPI's ``{name}`` and Flask's
#: ``<name>`` / ``:name`` styles map to a single wildcard.
_PATH_TEMPLATE_FASTAPI = re.compile(r"\{[^/}]+\}")
_PATH_TEMPLATE_COLON = re.compile(r":[^/]+")
_PATH_TEMPLATE_FLASK = re.compile(r"<(?:[A-Za-z_][A-Za-z_0-9]*:)?[^/>]+>")


# ---------------------------------------------------------------------------
# Pipeline-internal types — not part of the public schema.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendRoute:
    """One Python route handler discovered via decorators."""

    method: str  # uppercase HTTP method
    path_pattern: str
    node_id: str
    file: str
    line: int


@dataclass(frozen=True)
class FrontendHttpCall:
    """One TypeScript HTTP call discovered by static analysis.

    ``node_id`` is the graph id of the containing function, or the file
    id when the call is at module scope (the latter still gives the UI
    something to attach the edge to).
    """

    method: str  # uppercase
    url_pattern: str
    node_id: str
    file: str
    line: int
    snippet: str


# ---------------------------------------------------------------------------
# Backend route discovery
# ---------------------------------------------------------------------------


def discover_backend_routes(nodes: list[Node]) -> list[BackendRoute]:
    """Scan ``nodes`` for function-kind nodes with HTTP route decorators.

    Decorator strings come straight off :attr:`Node.decorators` — the
    Python parser captures the source verbatim (with leading ``@`` and
    full argument list). Pattern matching is regex-based — sufficient for
    the four supported decorator shapes; deliberately not a full Python
    AST parse of the decorator argument.
    """
    out: list[BackendRoute] = []
    for node in nodes:
        if node.kind != "function" or not node.decorators:
            continue
        if node.file is None or node.line_start is None:
            continue
        for deco in node.decorators:
            out.extend(_routes_from_decorator(deco, node))
    return out


def _routes_from_decorator(decorator: str, node: Node) -> list[BackendRoute]:
    """Parse one decorator string. Returns zero or more
    :class:`BackendRoute` instances (an ``api_route`` with two methods
    expands to two routes).
    """
    routes: list[BackendRoute] = []
    # ``api_route`` / Flask ``route`` with explicit methods first — they
    # take precedence over the simple verb form because both can match
    # the same prefix.
    for re_pattern in (_DECO_API_ROUTE_RE, _DECO_FLASK_ROUTE_RE):
        m = re_pattern.match(decorator)
        if m:
            path = m.group("path")
            methods = _METHOD_TOKEN_RE.findall(m.group("methods"))
            for method in methods:
                routes.append(
                    BackendRoute(
                        method=method.upper(),
                        path_pattern=path,
                        node_id=node.id,
                        file=node.file or "",
                        line=node.line_start or 0,
                    ),
                )
            if routes:
                return routes
            return []

    # Simple ``@<x>.get(...)`` shape.
    m = _DECO_VERB_RE.match(decorator)
    if m:
        routes.append(
            BackendRoute(
                method=m.group("verb").upper(),
                path_pattern=m.group("path"),
                node_id=node.id,
                file=node.file or "",
                line=node.line_start or 0,
            ),
        )
    return routes


# ---------------------------------------------------------------------------
# Frontend HTTP-call discovery
# ---------------------------------------------------------------------------


def discover_frontend_http_calls(
    *,
    rel_path: str,
    source: bytes,
    containing_nodes: dict[str, tuple[int, int]],
) -> list[FrontendHttpCall]:
    """Find every ``fetch(...)`` / ``axios.<verb>(...)`` call in the TS
    source whose URL argument is a plain string literal.

    Args:
        rel_path: Workspace-relative path of the TS file. Used to
            populate evidence and the fallback file-id containing-node.
        source: Raw file bytes.
        containing_nodes: Map of ``{node_id: (line_start, line_end)}``
            for functions/methods in the same file, so the matcher can
            attribute each call to a containing graph node.

    Tree-sitter is used directly; the call returns ``[]`` on parser
    exception so a single broken TS file never poisons the pass.
    """
    try:
        import tree_sitter_typescript
        from tree_sitter import Language, Parser
    except Exception as e:  # pragma: no cover - import-time defence
        log.warning("graph_http_match_tree_sitter_unavailable", error=str(e))
        return []

    try:
        if rel_path.endswith(".tsx"):
            lang = Language(tree_sitter_typescript.language_tsx())
        else:
            lang = Language(tree_sitter_typescript.language_typescript())
        parser = Parser(lang)
        tree = parser.parse(source)
    except Exception as e:
        log.warning("graph_http_match_parse_failed", file=rel_path, error=str(e))
        return []

    calls: list[FrontendHttpCall] = []
    try:
        _walk(tree.root_node, source, rel_path, containing_nodes, calls)
    except Exception as e:
        log.warning("graph_http_match_walk_failed", file=rel_path, error=str(e))
        return calls
    return calls


def _walk(
    node,
    source: bytes,
    rel_path: str,
    containing_nodes: dict[str, tuple[int, int]],
    out: list[FrontendHttpCall],
) -> None:
    if node.type == "call_expression":
        call = _extract_http_call(node, source, rel_path, containing_nodes)
        if call is not None:
            out.append(call)
    for child in node.children:
        _walk(child, source, rel_path, containing_nodes, out)


def _extract_http_call(
    node,
    source: bytes,
    rel_path: str,
    containing_nodes: dict[str, tuple[int, int]],
) -> FrontendHttpCall | None:
    """Try to interpret a tree-sitter ``call_expression`` as an HTTP
    request. Returns ``None`` for unrelated calls."""
    if not node.children:
        return None
    callee = node.children[0]
    args = _named_child(node, "arguments")
    if args is None:
        return None

    callee_text = source[callee.start_byte : callee.end_byte].decode(
        "utf-8",
        errors="replace",
    )
    method: str | None = None
    if callee.type == "identifier" and callee_text == "fetch":
        method = "GET"
    elif callee.type == "member_expression":
        obj, prop = _split_member(callee, source)
        if obj == "axios" and prop is not None and prop.upper() in _HTTP_METHODS:
            method = prop.upper()
    elif callee.type == "identifier" and callee_text == "axios":
        # Phase 4 accepts the explicit-config form too: ``axios("/x", {method: "POST"})``.
        # The method is parsed from the second argument's ``method:`` literal.
        method = _method_from_axios_config(args, source) or "GET"
    else:
        return None
    if method is None:
        return None

    url = _first_string_arg(args, source)
    if url is None:
        return None
    url = _normalise_url(url)

    line_no = node.start_point[0] + 1
    snippet = _line_text(source, node)
    node_id = _containing_node_id(rel_path, line_no, containing_nodes)
    return FrontendHttpCall(
        method=method,
        url_pattern=url,
        node_id=node_id,
        file=rel_path,
        line=line_no,
        snippet=snippet,
    )


def _method_from_axios_config(args_node, source: bytes) -> str | None:
    """Pull a ``method`` string from an ``axios("/x", {method: "POST"})`` call.

    Returns ``None`` when the second argument is missing, not a plain
    object literal, or doesn't have a literal ``method`` property.
    """
    found_url = False
    for child in args_node.children:
        if child.type == "string":
            found_url = True
            continue
        if found_url and child.type == "object":
            for pair in child.children:
                if pair.type == "pair":
                    key_node = pair.child_by_field_name("key")
                    val_node = pair.child_by_field_name("value")
                    if key_node is None or val_node is None:
                        continue
                    key_text = source[key_node.start_byte : key_node.end_byte].decode()
                    if key_text.strip("\"'`") == "method" and val_node.type == "string":
                        for inner in val_node.children:
                            if inner.type == "string_fragment":
                                return source[inner.start_byte : inner.end_byte].decode().upper()
    return None


def _first_string_arg(args_node, source: bytes) -> str | None:
    """Pull the first argument's literal-string value. Returns ``None``
    for non-string / template-with-substitution / no-arg cases."""
    for c in args_node.children:
        if c.type == "string":
            for inner in c.children:
                if inner.type == "string_fragment":
                    return source[inner.start_byte : inner.end_byte].decode(
                        "utf-8",
                        errors="replace",
                    )
            return None
        if c.type == "template_string":
            has_subst = any(cc.type == "template_substitution" for cc in c.children)
            if has_subst:
                return None
            for inner in c.children:
                if inner.type == "string_fragment":
                    return source[inner.start_byte : inner.end_byte].decode(
                        "utf-8",
                        errors="replace",
                    )
            return None
        if c.type in ("(", ",", ")"):
            continue
        # Anything else as the first arg → not a literal URL.
        return None
    return None


def _split_member(node, source: bytes) -> tuple[str | None, str | None]:
    obj: str | None = None
    prop: str | None = None
    for c in node.children:
        if c.type == "identifier" and obj is None:
            obj = source[c.start_byte : c.end_byte].decode()
        elif c.type == "property_identifier":
            prop = source[c.start_byte : c.end_byte].decode()
    return (obj, prop)


def _named_child(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _line_text(source: bytes, node) -> str:
    row = node.start_point[0]
    lines = source.split(b"\n")
    if row >= len(lines):
        return ""
    return lines[row].decode("utf-8", errors="replace").strip()


def _containing_node_id(
    rel_path: str,
    line: int,
    containing_nodes: dict[str, tuple[int, int]],
) -> str:
    """Find the function/method node whose range covers ``line``; fall
    back to the file id. When multiple ranges overlap (nested), prefer
    the innermost (smallest span)."""
    best_id: str | None = None
    best_span: int | None = None
    for node_id, (start, end) in containing_nodes.items():
        if start <= line <= end:
            span = end - start
            if best_span is None or span < best_span:
                best_id = node_id
                best_span = span
    if best_id is not None:
        return best_id
    return f"file:{rel_path}"


def _normalise_url(url: str) -> str:
    """Strip ``?query`` and a trailing ``/`` (except for the root)."""
    if "?" in url:
        url = url.split("?", 1)[0]
    if len(url) > 1 and url.endswith("/"):
        url = url[:-1]
    return url


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _path_matches(route_pattern: str, call_url: str) -> bool:
    """Treat ``{param}`` and ``:param`` and ``<param>`` segments as ``*``
    wildcards and compare segment-by-segment.
    """
    route_norm = _normalise_url(route_pattern)
    call_norm = _normalise_url(call_url)
    route_segments = route_norm.split("/")
    call_segments = call_norm.split("/")
    if len(route_segments) != len(call_segments):
        return False
    for r, c in zip(route_segments, call_segments, strict=False):
        if _is_template_segment(r):
            continue
        if _is_template_segment(c):
            # If the frontend URL itself uses templating, accept it.
            continue
        if r != c:
            return False
    return True


def _is_template_segment(seg: str) -> bool:
    if not seg:
        return False
    if _PATH_TEMPLATE_FASTAPI.fullmatch(seg):
        return True
    if _PATH_TEMPLATE_FLASK.fullmatch(seg):
        return True
    return seg.startswith(":")


class _LLMPickPayload(BaseModel):
    target_node_id: str = Field(min_length=1)


def _build_llm_system_prompt(
    call: FrontendHttpCall,
    candidates: list[BackendRoute],
) -> str:
    bulleted = "\n".join(
        f"- {r.node_id}  ({r.method} {r.path_pattern}, {r.file}:{r.line})" for r in candidates
    )
    return (
        "You disambiguate cross-language HTTP edges between a TypeScript "
        "frontend call and one of several Python backend route handlers.\n"
        "\n"
        "You will see one frontend call (method + URL + surrounding line) and "
        "a list of backend route candidates that all share the same HTTP "
        "method and a compatible URL pattern. Pick the single best target.\n"
        "\n"
        "OUTPUT — return ONLY a JSON object with this shape, no prose, no "
        "markdown fences:\n"
        '{"target_node_id": "<id from the candidate list below>"}\n'
        "\n"
        "Rules:\n"
        "1. target_node_id must appear in the candidate list verbatim.\n"
        "2. If you cannot decide, pick the candidate with the most specific "
        "literal-prefix match; ties resolve to the first candidate listed.\n"
        "\n"
        "Candidate backend routes:\n"
        f"{bulleted}\n"
    )


def _build_llm_user_message(call: FrontendHttpCall) -> Message:
    return Message(
        role="user",
        content=(
            f"Frontend call site: {call.file}:{call.line}\n"
            f"Method: {call.method}\n"
            f"URL: {call.url_pattern}\n"
            f"Snippet: {call.snippet}\n"
        ),
    )


async def _llm_pick_target(
    *,
    provider: LLMProvider,
    call: FrontendHttpCall,
    candidates: list[BackendRoute],
) -> str | None:
    try:
        payload = await complete_json(
            provider,
            messages=[_build_llm_user_message(call)],
            system=_build_llm_system_prompt(call, candidates),
            max_tokens=256,
            temperature=0.0,
        )
    except Exception as e:
        log.warning(
            "graph_http_match_llm_error",
            call=call.node_id,
            error=str(e),
            error_type=e.__class__.__name__,
        )
        return None
    try:
        parsed = _LLMPickPayload(**payload)
    except ValidationError:
        return None
    return parsed.target_node_id


async def match_http_edges(
    *,
    workspace_path: str,
    nodes: list[Node],
    provider: LLMProvider | None = None,
) -> list[Edge]:
    """Run the full HTTP-matching pass against ``workspace_path``.

    The caller (the pipeline) hands us the assembled node list (already
    including decorator metadata) and an optional LLM provider for
    disambiguating ambiguous matches. Returns the list of new
    :class:`Edge` objects to append to the blob; the existing edge set
    is untouched.

    Validation:
      * unambiguous AST edges are *not* run through ``validate_citation``
        because their evidence file is the workspace file that
        ``discover_frontend_http_calls`` just read, and we trust our own
        line numbers;
      * LLM-resolved edges go through ``validate_citation`` (against the
        frontend call line) and ``validate_target`` (against the node
        set), matching ADR-016's load-bearing trust rule.
    """
    routes = discover_backend_routes(nodes)
    if not routes:
        return []

    # Group route lookup by method for quick filtering.
    routes_by_method: dict[str, list[BackendRoute]] = {}
    for r in routes:
        routes_by_method.setdefault(r.method, []).append(r)

    # Collect TS files + a per-file containing-node map.
    ts_nodes_by_file: dict[str, dict[str, tuple[int, int]]] = {}
    for n in nodes:
        if n.file is None:
            continue
        if not (n.file.endswith(".ts") or n.file.endswith(".tsx")):
            continue
        if n.kind not in ("function", "class"):
            continue
        if n.line_start is None or n.line_end is None:
            continue
        ts_nodes_by_file.setdefault(n.file, {})[n.id] = (n.line_start, n.line_end)
    # Ensure every TS file we'll scan is keyed, even if it has zero
    # functions (the call still gets attributed to the file).
    for n in nodes:
        if (
            n.file is not None
            and (n.file.endswith(".ts") or n.file.endswith(".tsx"))
            and n.file not in ts_nodes_by_file
        ):
            ts_nodes_by_file[n.file] = {}

    edges: list[Edge] = []
    for rel_path, containing_nodes in ts_nodes_by_file.items():
        abs_path = os.path.join(workspace_path, rel_path)
        try:
            with open(abs_path, "rb") as fh:
                src = fh.read()
        except OSError as e:
            log.info(
                "graph_http_match_ts_unreadable",
                file=rel_path,
                error=str(e),
            )
            continue
        try:
            calls = discover_frontend_http_calls(
                rel_path=rel_path,
                source=src,
                containing_nodes=containing_nodes,
            )
        except Exception as e:
            log.warning(
                "graph_http_match_discover_failed",
                file=rel_path,
                error=str(e),
            )
            continue

        for call in calls:
            candidates = [
                r
                for r in routes_by_method.get(call.method, ())
                if _path_matches(r.path_pattern, call.url_pattern)
            ]
            if not candidates:
                continue
            if len(candidates) == 1:
                edges.append(_build_ast_edge(call, candidates[0]))
                continue
            # Ambiguous — need the LLM.
            if provider is None:
                log.info(
                    "graph_http_match_ambiguous_no_provider",
                    call=call.node_id,
                    candidates=[r.node_id for r in candidates],
                )
                continue
            chosen = await _llm_pick_target(
                provider=provider,
                call=call,
                candidates=candidates,
            )
            if chosen is None:
                continue
            picked = next((r for r in candidates if r.node_id == chosen), None)
            if picked is None:
                log.info(
                    "graph_http_match_llm_target_outside_candidates",
                    call=call.node_id,
                    chosen=chosen,
                )
                continue
            llm_edge = _build_llm_edge(call, picked)
            if not validate_citation(workspace_path, llm_edge):
                continue
            if not validate_target(llm_edge, nodes):
                continue
            edges.append(llm_edge)
    return edges


def _build_ast_edge(call: FrontendHttpCall, route: BackendRoute) -> Edge:
    return Edge(
        source=call.node_id,
        target=route.node_id,
        kind="http",
        evidence=EdgeEvidence(file=call.file, line=call.line, snippet=call.snippet),
        source_kind="ast",
    )


def _build_llm_edge(call: FrontendHttpCall, route: BackendRoute) -> Edge:
    return Edge(
        source=call.node_id,
        target=route.node_id,
        kind="http",
        evidence=EdgeEvidence(file=call.file, line=call.line, snippet=call.snippet),
        source_kind="llm",
    )


__all__ = [
    "BackendRoute",
    "FrontendHttpCall",
    "discover_backend_routes",
    "discover_frontend_http_calls",
    "match_http_edges",
]
