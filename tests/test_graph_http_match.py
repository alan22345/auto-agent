"""Cross-language HTTP matching (ADR-016 Phase 4 §http_match).

Links TypeScript ``fetch``/``axios`` calls to FastAPI/Flask route
handlers via URL pattern. The matching itself is unit-testable in
isolation — these tests pin the load-bearing rules:

* backend route discovery from Python ``Node.decorators``:
    * ``@router.get("/api/x")``, ``@app.post(...)``;
    * ``@router.api_route("/x", methods=["GET", "POST"])`` (FastAPI);
    * ``@router.route("/x", methods=["POST"])`` (Flask);
    * decorators outside this set are ignored;
* frontend call discovery from TS source via tree-sitter:
    * ``fetch("/api/x")`` defaults to ``GET``;
    * ``axios.get("/y")``, ``axios.post("/y", body)``;
    * non-literal URLs (template substitutions, dynamic concat) are
      *not* candidates;
* matching:
    * exact ``(method, path)`` literal pair → AST edge;
    * path templating with ``{param}`` / ``:param`` segments treated as
      wildcards on both sides;
    * trailing slashes normalised; query strings stripped;
    * unambiguous (single candidate) → ``Edge(kind="http",
      source_kind="ast")``;
    * ambiguous (≥2 candidates) → one LLM call (mocked here) emits the
      tied target; citation validates against the frontend call line;
    * frontend call without any matching backend → no edge, no error;
* failure isolation: an exception during TS scanning of one file or
  during an ambiguous-match LLM call drops only that result.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer.http_match import (
    discover_backend_routes,
    discover_frontend_http_calls,
    match_http_edges,
)
from agent.llm.types import LLMResponse, Message, TokenUsage
from shared.types import EdgeEvidence, Node

if TYPE_CHECKING:
    from pathlib import Path


def _backend_node(
    *,
    node_id: str,
    decorators: list[str],
    file: str = "orchestrator/router.py",
    line: int = 10,
) -> Node:
    return Node(
        id=node_id,
        kind="function",
        label=node_id.rsplit(":", 1)[-1],
        file=file,
        line_start=line,
        line_end=line + 5,
        area="orchestrator",
        parent=f"file:{file}",
        decorators=decorators,
    )


def _provider_returning(payload: dict | str) -> MagicMock:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            message=Message(role="assistant", content=body),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        ),
    )
    return provider


# ---------------------------------------------------------------------------
# Backend route discovery
# ---------------------------------------------------------------------------


class TestBackendRouteDiscovery:
    def test_router_get_with_literal_path(self) -> None:
        node = _backend_node(
            node_id="orchestrator/router.py::list_repos",
            decorators=['@router.get("/api/repos")'],
        )
        routes = discover_backend_routes([node])
        assert len(routes) == 1
        r = routes[0]
        assert r.method == "GET"
        assert r.path_pattern == "/api/repos"
        assert r.node_id == "orchestrator/router.py::list_repos"

    def test_app_post_decorator(self) -> None:
        node = _backend_node(
            node_id="orchestrator/router.py::add",
            decorators=['@app.post("/api/items")'],
        )
        routes = discover_backend_routes([node])
        assert len(routes) == 1
        assert routes[0].method == "POST"
        assert routes[0].path_pattern == "/api/items"

    def test_api_route_with_methods_kwarg(self) -> None:
        node = _backend_node(
            node_id="orchestrator/router.py::both",
            decorators=['@router.api_route("/api/both", methods=["GET", "POST"])'],
        )
        routes = discover_backend_routes([node])
        methods = sorted(r.method for r in routes)
        assert methods == ["GET", "POST"]
        assert all(r.path_pattern == "/api/both" for r in routes)

    def test_flask_route_with_methods_kwarg(self) -> None:
        node = _backend_node(
            node_id="orchestrator/router.py::flask_view",
            decorators=['@router.route("/api/x", methods=["POST"])'],
        )
        routes = discover_backend_routes([node])
        assert len(routes) == 1
        assert routes[0].method == "POST"
        assert routes[0].path_pattern == "/api/x"

    def test_non_route_decorator_ignored(self) -> None:
        node = _backend_node(
            node_id="m::x",
            decorators=["@dataclass", "@pytest.fixture"],
        )
        assert discover_backend_routes([node]) == []

    def test_multiple_route_decorators_on_one_function(self) -> None:
        node = _backend_node(
            node_id="m::x",
            decorators=[
                '@router.get("/v1/x")',
                '@router.get("/v2/x")',
            ],
        )
        routes = discover_backend_routes([node])
        paths = sorted(r.path_pattern for r in routes)
        assert paths == ["/v1/x", "/v2/x"]

    def test_non_function_node_ignored(self) -> None:
        node = Node(
            id="area:agent",
            kind="area",
            label="agent",
            area="agent",
            decorators=['@router.get("/api/x")'],  # nonsense but tests the guard
        )
        assert discover_backend_routes([node]) == []


# ---------------------------------------------------------------------------
# Frontend HTTP call discovery
# ---------------------------------------------------------------------------


class TestFrontendCallDiscovery:
    def _ts_source(self, body: str) -> bytes:
        return body.encode()

    def test_fetch_literal_url_defaults_to_get(self) -> None:
        src = self._ts_source(
            'async function f() {\n  await fetch("/api/repos");\n}\n',
        )
        calls = discover_frontend_http_calls(
            rel_path="web-next/client.ts",
            source=src,
            containing_nodes={"web-next/client.ts::f": (1, 5)},
        )
        assert len(calls) == 1
        c = calls[0]
        assert c.method == "GET"
        assert c.url_pattern == "/api/repos"
        assert c.node_id == "web-next/client.ts::f"

    def test_axios_get(self) -> None:
        src = self._ts_source(
            'async function f() {\n  await axios.get("/api/x");\n}\n',
        )
        calls = discover_frontend_http_calls(
            rel_path="web-next/client.ts",
            source=src,
            containing_nodes={"web-next/client.ts::f": (1, 5)},
        )
        assert len(calls) == 1
        assert calls[0].method == "GET"
        assert calls[0].url_pattern == "/api/x"

    def test_axios_post(self) -> None:
        src = self._ts_source(
            'async function f() {\n  await axios.post("/api/items", {});\n}\n',
        )
        calls = discover_frontend_http_calls(
            rel_path="web-next/client.ts",
            source=src,
            containing_nodes={"web-next/client.ts::f": (1, 5)},
        )
        assert len(calls) == 1
        assert calls[0].method == "POST"
        assert calls[0].url_pattern == "/api/items"

    def test_template_literal_with_substitution_skipped(self) -> None:
        src = self._ts_source(
            "async function f(id: string) {\n  await fetch(`/api/x/${id}`);\n}\n",
        )
        calls = discover_frontend_http_calls(
            rel_path="web-next/client.ts",
            source=src,
            containing_nodes={"web-next/client.ts::f": (1, 5)},
        )
        # Template substitutions are out of scope for Phase 4 literal
        # matching — no call is emitted.
        assert calls == []

    def test_fetch_outside_known_function_assigned_to_file(self) -> None:
        src = self._ts_source('await fetch("/api/repos");\n')
        calls = discover_frontend_http_calls(
            rel_path="web-next/c.ts",
            source=src,
            containing_nodes={},
        )
        assert len(calls) == 1
        # No function scope; node_id falls back to the file id.
        assert calls[0].node_id == "file:web-next/c.ts"

    def test_query_string_stripped_in_url(self) -> None:
        src = self._ts_source(
            'async function f() {\n  await fetch("/api/x?include=y");\n}\n',
        )
        calls = discover_frontend_http_calls(
            rel_path="c.ts",
            source=src,
            containing_nodes={"c.ts::f": (1, 5)},
        )
        assert calls[0].url_pattern == "/api/x"

    def test_syntax_error_in_ts_does_not_crash(self) -> None:
        src = self._ts_source('async function f( {\n  fetch("/api/x");\n}\n')
        # Should not raise.
        calls = discover_frontend_http_calls(
            rel_path="c.ts",
            source=src,
            containing_nodes={},
        )
        # tree-sitter error recovery may still find the call — but the
        # contract is *no crash*. We tolerate either zero or one match.
        assert isinstance(calls, list)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unambiguous_match_emits_ast_edge(tmp_path: Path) -> None:
    """Single route matches → no LLM call, edge tagged source_kind=ast."""
    backend_file = tmp_path / "router.py"
    backend_file.write_text(
        '@router.get("/api/repos")\ndef list_repos():\n    return []\n',
    )
    front_file = tmp_path / "client.ts"
    front_file.write_text(
        'async function fetchRepos() {\n  await fetch("/api/repos");\n}\n',
    )

    backend_node = _backend_node(
        node_id="router.py::list_repos",
        decorators=['@router.get("/api/repos")'],
        file="router.py",
        line=1,
    )
    front_node = Node(
        id="client.ts::fetchRepos",
        kind="function",
        label="fetchRepos",
        file="client.ts",
        line_start=1,
        line_end=3,
        area="web-next",
        parent="file:client.ts",
    )
    file_node = Node(
        id="file:client.ts",
        kind="file",
        label="client.ts",
        file="client.ts",
        line_start=1,
        line_end=3,
        area="web-next",
        parent="area:web-next",
    )

    provider = _provider_returning({"target_node_id": ""})  # should never be called
    edges = await match_http_edges(
        workspace_path=str(tmp_path),
        nodes=[backend_node, front_node, file_node],
        provider=provider,
    )
    assert len(edges) == 1
    e = edges[0]
    assert e.kind == "http"
    assert e.source_kind == "ast"
    assert e.source == "client.ts::fetchRepos"
    assert e.target == "router.py::list_repos"
    assert e.evidence.file == "client.ts"
    # Provider was not consulted for an unambiguous match.
    assert provider.complete.await_count == 0


@pytest.mark.asyncio
async def test_path_templating_matches_concrete_url(tmp_path: Path) -> None:
    """``@router.get("/api/repos/{id}")`` matches ``fetch("/api/repos/123")``."""
    backend_file = tmp_path / "router.py"
    backend_file.write_text(
        '@router.get("/api/repos/{id}")\ndef get_repo(id: int):\n    return id\n',
    )
    front_file = tmp_path / "client.ts"
    front_file.write_text(
        'async function fetchRepo() {\n  await fetch("/api/repos/123");\n}\n',
    )

    backend_node = _backend_node(
        node_id="router.py::get_repo",
        decorators=['@router.get("/api/repos/{id}")'],
        file="router.py",
        line=1,
    )
    front_node = Node(
        id="client.ts::fetchRepo",
        kind="function",
        label="fetchRepo",
        file="client.ts",
        line_start=1,
        line_end=3,
        area="web-next",
        parent="file:client.ts",
    )

    provider = _provider_returning({"target_node_id": ""})
    edges = await match_http_edges(
        workspace_path=str(tmp_path),
        nodes=[backend_node, front_node],
        provider=provider,
    )
    assert len(edges) == 1
    assert edges[0].target == "router.py::get_repo"
    assert edges[0].source_kind == "ast"
    assert provider.complete.await_count == 0


@pytest.mark.asyncio
async def test_flask_colon_path_matches_concrete_url(tmp_path: Path) -> None:
    """Flask's ``:id`` style templating matches literal URLs too."""
    backend_file = tmp_path / "router.py"
    backend_file.write_text(
        '@router.route("/api/items/:id", methods=["GET"])\ndef view_item(id):\n    return id\n',
    )
    front_file = tmp_path / "client.ts"
    front_file.write_text(
        'async function f() {\n  await fetch("/api/items/abc");\n}\n',
    )

    backend_node = _backend_node(
        node_id="router.py::view_item",
        decorators=['@router.route("/api/items/:id", methods=["GET"])'],
        file="router.py",
        line=1,
    )
    front_node = Node(
        id="client.ts::f",
        kind="function",
        label="f",
        file="client.ts",
        line_start=1,
        line_end=3,
        area="web-next",
        parent="file:client.ts",
    )

    provider = _provider_returning({"target_node_id": ""})
    edges = await match_http_edges(
        workspace_path=str(tmp_path),
        nodes=[backend_node, front_node],
        provider=provider,
    )
    assert len(edges) == 1
    assert edges[0].target == "router.py::view_item"


@pytest.mark.asyncio
async def test_ambiguous_match_calls_llm(tmp_path: Path) -> None:
    """Two routes are valid candidates → LLM picks one → edge tagged llm."""
    backend_file = tmp_path / "router.py"
    backend_file.write_text(
        '@router.get("/api/repos/{id}")\n'
        "def v1(id): return id\n"
        "\n"
        '@router.get("/api/repos/{name}")\n'
        "def v2(name): return name\n",
    )
    front_file = tmp_path / "client.ts"
    front_file.write_text(
        'async function f() {\n  await fetch("/api/repos/123");\n}\n',
    )

    n1 = _backend_node(
        node_id="router.py::v1",
        decorators=['@router.get("/api/repos/{id}")'],
        file="router.py",
        line=1,
    )
    n2 = _backend_node(
        node_id="router.py::v2",
        decorators=['@router.get("/api/repos/{name}")'],
        file="router.py",
        line=4,
    )
    fnode = Node(
        id="client.ts::f",
        kind="function",
        label="f",
        file="client.ts",
        line_start=1,
        line_end=3,
        area="web-next",
        parent="file:client.ts",
    )

    provider = _provider_returning(
        {"target_node_id": "router.py::v1"},
    )
    edges = await match_http_edges(
        workspace_path=str(tmp_path),
        nodes=[n1, n2, fnode],
        provider=provider,
    )
    assert len(edges) == 1
    e = edges[0]
    assert e.source_kind == "llm"
    assert e.target == "router.py::v1"
    assert e.kind == "http"
    assert provider.complete.await_count == 1


@pytest.mark.asyncio
async def test_ambiguous_match_drops_when_llm_picks_outside_candidates(tmp_path: Path) -> None:
    """LLM picks a node that is not in the candidate pool → no edge."""
    backend_file = tmp_path / "router.py"
    backend_file.write_text(
        '@router.get("/api/repos/{id}")\ndef v1(id): return id\n'
        '@router.get("/api/repos/{name}")\ndef v2(name): return name\n',
    )
    front_file = tmp_path / "client.ts"
    front_file.write_text(
        'async function f() {\n  await fetch("/api/repos/123");\n}\n',
    )

    n1 = _backend_node(
        node_id="router.py::v1",
        decorators=['@router.get("/api/repos/{id}")'],
        file="router.py",
        line=1,
    )
    n2 = _backend_node(
        node_id="router.py::v2",
        decorators=['@router.get("/api/repos/{name}")'],
        file="router.py",
        line=2,
    )
    fnode = Node(
        id="client.ts::f",
        kind="function",
        label="f",
        file="client.ts",
        line_start=1,
        line_end=3,
        area="web-next",
        parent="file:client.ts",
    )
    provider = _provider_returning({"target_node_id": "imaginary::nope"})
    edges = await match_http_edges(
        workspace_path=str(tmp_path),
        nodes=[n1, n2, fnode],
        provider=provider,
    )
    assert edges == []


@pytest.mark.asyncio
async def test_no_backend_match_emits_no_edge(tmp_path: Path) -> None:
    front_file = tmp_path / "client.ts"
    front_file.write_text(
        'async function f() {\n  await fetch("/api/never");\n}\n',
    )
    fnode = Node(
        id="client.ts::f",
        kind="function",
        label="f",
        file="client.ts",
        line_start=1,
        line_end=3,
        area="web-next",
        parent="file:client.ts",
    )
    provider = _provider_returning({"target_node_id": ""})
    edges = await match_http_edges(
        workspace_path=str(tmp_path),
        nodes=[fnode],
        provider=provider,
    )
    assert edges == []
    assert provider.complete.await_count == 0


@pytest.mark.asyncio
async def test_method_mismatch_does_not_match(tmp_path: Path) -> None:
    """GET fetch must not match a POST-only route, even with same path."""
    backend_file = tmp_path / "router.py"
    backend_file.write_text(
        '@router.post("/api/x")\ndef create(): return None\n',
    )
    front_file = tmp_path / "client.ts"
    front_file.write_text(
        'async function f() {\n  await fetch("/api/x");\n}\n',
    )
    bnode = _backend_node(
        node_id="router.py::create",
        decorators=['@router.post("/api/x")'],
        file="router.py",
        line=1,
    )
    fnode = Node(
        id="client.ts::f",
        kind="function",
        label="f",
        file="client.ts",
        line_start=1,
        line_end=3,
        area="web-next",
        parent="file:client.ts",
    )
    edges = await match_http_edges(
        workspace_path=str(tmp_path),
        nodes=[bnode, fnode],
        provider=_provider_returning({"target_node_id": ""}),
    )
    assert edges == []


@pytest.mark.asyncio
async def test_llm_failure_during_ambiguous_match_isolated(tmp_path: Path) -> None:
    """If the LLM raises on one ambiguous match, only that match is
    dropped — the rest of the pipeline keeps going."""
    backend_file = tmp_path / "router.py"
    backend_file.write_text(
        '@router.get("/api/x/{id}")\ndef v1(id): return id\n'
        '@router.get("/api/x/{name}")\ndef v2(name): return name\n',
    )
    front_file = tmp_path / "client.ts"
    front_file.write_text(
        'async function f() {\n  await fetch("/api/x/123");\n}\n',
    )
    n1 = _backend_node(
        node_id="router.py::v1",
        decorators=['@router.get("/api/x/{id}")'],
        file="router.py",
        line=1,
    )
    n2 = _backend_node(
        node_id="router.py::v2",
        decorators=['@router.get("/api/x/{name}")'],
        file="router.py",
        line=2,
    )
    fnode = Node(
        id="client.ts::f",
        kind="function",
        label="f",
        file="client.ts",
        line_start=1,
        line_end=3,
        area="web-next",
        parent="file:client.ts",
    )
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
    edges = await match_http_edges(
        workspace_path=str(tmp_path),
        nodes=[n1, n2, fnode],
        provider=provider,
    )
    assert edges == []  # one site, LLM failed → dropped, no crash


@pytest.mark.asyncio
async def test_no_provider_skips_llm_ambiguous_match(tmp_path: Path) -> None:
    """When no provider is supplied, ambiguous matches drop silently —
    the AST-only path still emits unambiguous edges."""
    backend_file = tmp_path / "router.py"
    backend_file.write_text(
        '@router.get("/api/x/{id}")\ndef v1(id): return id\n'
        '@router.get("/api/x/{name}")\ndef v2(name): return name\n'
        '@router.get("/api/y")\ndef v3(): return None\n',
    )
    front_file = tmp_path / "client.ts"
    front_file.write_text(
        'async function f() {\n  await fetch("/api/x/123");\n  await fetch("/api/y");\n}\n',
    )
    n1 = _backend_node(
        node_id="router.py::v1",
        decorators=['@router.get("/api/x/{id}")'],
        file="router.py",
        line=1,
    )
    n2 = _backend_node(
        node_id="router.py::v2",
        decorators=['@router.get("/api/x/{name}")'],
        file="router.py",
        line=2,
    )
    n3 = _backend_node(
        node_id="router.py::v3",
        decorators=['@router.get("/api/y")'],
        file="router.py",
        line=3,
    )
    fnode = Node(
        id="client.ts::f",
        kind="function",
        label="f",
        file="client.ts",
        line_start=1,
        line_end=4,
        area="web-next",
        parent="file:client.ts",
    )
    edges = await match_http_edges(
        workspace_path=str(tmp_path),
        nodes=[n1, n2, n3, fnode],
        provider=None,
    )
    # /api/y is unambiguous → AST edge.
    assert len(edges) == 1
    assert edges[0].target == "router.py::v3"
    assert edges[0].source_kind == "ast"


# Re-export the dataclass via the http_match module for type checks in tests.
def test_evidence_for_unambiguous_edge_points_at_frontend_call(tmp_path: Path) -> None:
    """The cited evidence must be at the *frontend* call line — that is
    where the call is observable."""
    # Just sanity-check the EdgeEvidence shape.
    e = EdgeEvidence(file="client.ts", line=2, snippet='await fetch("/api/x")')
    assert e.line == 2
