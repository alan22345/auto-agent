"""Query the code graph for a repo (ADR-016 Phase 6 §12).

A single agent tool with seven read-only ops over the latest stored
:class:`shared.types.RepoGraphBlob` for a repo. Every response carries
a staleness envelope (graph SHA vs. workspace HEAD) and per-result
existence flags so the agent can filter out symbols that have been
deleted or renamed in its current branch.

The tool only reads. It never refreshes the graph (Phase 7), never
caches results (deferred), and never has any side effects beyond the
DB SELECT + the workspace ``git rev-parse`` invocation that
:func:`agent.graph_analyzer.staleness.compute_staleness` performs.

Returns a single :class:`agent.tools.base.ToolResult` whose ``output``
field is a JSON string with the shape::

    {
      "op": "<op-name>",
      "staleness": {
        "graph_sha": "<RepoGraph.commit_sha>",
        "workspace_sha": "<HEAD of analyser workspace, or null>",
        "drifted": <bool>
      },
      "result": <op-specific raw payload>,
      "results_with_existence": [   # only for list-returning ops
        {"value": <Node|Edge dict>, "exists_in_workspace": <bool>}
      ]
    }

Ops returning a single value (``path_between``, ``violates_boundaries``)
omit ``results_with_existence`` — the agent reads ``result`` directly.
"""

from __future__ import annotations

import json
import os
from collections import deque
from typing import Any, ClassVar

import structlog
from sqlalchemy import select

from agent.graph_analyzer.staleness import compute_staleness
from agent.tools.base import Tool, ToolContext, ToolResult
from shared.database import async_session
from shared.models import RepoGraph, RepoGraphConfig
from shared.types import Edge, Node, RepoGraphBlob

log = structlog.get_logger(__name__)


_KNOWN_OPS: frozenset[str] = frozenset(
    {
        "callers_of",
        "callees_of",
        "outgoing_edges",
        "incoming_edges",
        "public_surface",
        "path_between",
        "violates_boundaries",
    },
)


class QueryRepoGraphTool(Tool):
    name = "query_repo_graph"
    description = (
        "Query a repo's pre-analysed code graph for structural facts: who "
        "calls a function, what does it call, what's an area's public "
        "surface, is there a path between two symbols, does an edge cross "
        "a boundary. Faster and more exact than grepping. Every response "
        "carries staleness flags so you can decide whether to trust the "
        "result against your current task branch."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "repo_id": {
                "type": "integer",
                "description": "Numeric id of the repo whose graph to query.",
            },
            "op": {
                "type": "string",
                "enum": sorted(_KNOWN_OPS),
                "description": (
                    "Which graph query to run. "
                    "'callers_of'/'callees_of' return nodes; "
                    "'outgoing_edges'/'incoming_edges' return raw edges "
                    "with evidence; 'public_surface' returns nodes in an "
                    "area's public API; 'path_between' returns a list of "
                    "node ids; 'violates_boundaries' returns a single edge "
                    "dict or null."
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    "Op-specific parameters. Shapes: "
                    "callers_of/callees_of/outgoing_edges/incoming_edges "
                    "take {node_id}; public_surface takes {area_name}; "
                    "path_between takes {source_id, target_id, "
                    "max_depth?=5}; violates_boundaries takes "
                    "{source_id, target_id}."
                ),
            },
        },
        "required": ["repo_id", "op", "params"],
    }
    is_readonly = True

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        repo_id = arguments.get("repo_id")
        op = arguments.get("op")
        params = arguments.get("params") or {}

        if not isinstance(repo_id, int):
            return ToolResult(
                output="Error: 'repo_id' (integer) is required.",
                is_error=True,
            )
        if not isinstance(op, str) or not op:
            return ToolResult(
                output="Error: 'op' (string) is required.",
                is_error=True,
            )
        if op not in _KNOWN_OPS:
            return ToolResult(
                output=(f"Error: unknown op '{op}'. Valid ops: {sorted(_KNOWN_OPS)}."),
                is_error=True,
            )
        if not isinstance(params, dict):
            return ToolResult(
                output="Error: 'params' must be an object.",
                is_error=True,
            )

        # 1. Load config + latest graph row.
        try:
            cfg, graph_row = await _load_graph(repo_id)
        except Exception as e:
            log.warning(
                "query_repo_graph_db_failed",
                repo_id=repo_id,
                op=op,
                error=str(e),
            )
            return ToolResult(
                output=f"Error loading graph for repo {repo_id}: {e}",
                is_error=True,
            )
        if cfg is None or cfg.last_analysis_id is None or graph_row is None:
            return ToolResult(
                output=(
                    f"No graph available for repo {repo_id} — onboard the "
                    "repo at /code-graph or wait for analysis to complete."
                ),
                is_error=True,
            )

        # 2. Deserialise the blob.
        try:
            blob = RepoGraphBlob.model_validate(graph_row.graph_json)
        except Exception as e:
            log.warning(
                "query_repo_graph_blob_invalid",
                repo_id=repo_id,
                op=op,
                error=str(e),
            )
            return ToolResult(
                output=f"Error: stored graph for repo {repo_id} is invalid: {e}",
                is_error=True,
            )

        workspace_path = cfg.workspace_path or ""

        # 3. Compute staleness envelope.
        staleness = compute_staleness(
            graph_sha=graph_row.commit_sha,
            workspace_path=workspace_path,
        )

        # 4. Dispatch to per-op handler.
        try:
            payload = _dispatch(
                op=op,
                params=params,
                blob=blob,
                workspace_path=workspace_path,
            )
        except _OpError as e:
            return ToolResult(output=f"Error: {e}", is_error=True)

        envelope: dict[str, Any] = {
            "op": op,
            "staleness": {
                "graph_sha": staleness.graph_sha,
                "workspace_sha": staleness.workspace_sha,
                "drifted": staleness.drifted,
            },
        }
        envelope.update(payload)

        body = json.dumps(envelope, ensure_ascii=False)
        return ToolResult(output=body, token_estimate=len(body) // 3)


# ---------------------------------------------------------------------------
# DB loader
# ---------------------------------------------------------------------------


async def _load_graph(repo_id: int) -> tuple[Any, Any]:
    """Return ``(config_row, graph_row)`` for ``repo_id``.

    Either may be ``None`` — the caller decides whether that's an error
    state. Two-step query because the config row carries
    ``last_analysis_id``; loading the graph row needs that id.
    """
    async with async_session() as session:
        result = await session.execute(
            select(RepoGraphConfig).where(
                RepoGraphConfig.repo_id == repo_id,
            ),
        )
        cfg = result.scalar_one_or_none()
        if cfg is None or cfg.last_analysis_id is None:
            return cfg, None

        result = await session.execute(
            select(RepoGraph).where(RepoGraph.id == cfg.last_analysis_id),
        )
        graph_row = result.scalar_one_or_none()
        return cfg, graph_row


# ---------------------------------------------------------------------------
# Op dispatch
# ---------------------------------------------------------------------------


class _OpError(Exception):
    """Raised by per-op handlers to surface a user-readable error."""


def _dispatch(
    *,
    op: str,
    params: dict[str, Any],
    blob: RepoGraphBlob,
    workspace_path: str,
) -> dict[str, Any]:
    """Run ``op`` over ``blob``; return the partial envelope (no staleness)."""
    if op in ("callers_of", "callees_of"):
        node_id = _require_str(params, "node_id")
        nodes = _callers_of(blob, node_id) if op == "callers_of" else _callees_of(blob, node_id)
        return _wrap_nodes(nodes, workspace_path)

    if op in ("outgoing_edges", "incoming_edges"):
        node_id = _require_str(params, "node_id")
        edges = (
            _outgoing_edges(blob, node_id)
            if op == "outgoing_edges"
            else _incoming_edges(blob, node_id)
        )
        return _wrap_edges(edges, blob, workspace_path)

    if op == "public_surface":
        area = _require_str(params, "area_name")
        nodes = _public_surface(blob, area)
        return _wrap_nodes(nodes, workspace_path)

    if op == "path_between":
        source_id = _require_str(params, "source_id")
        target_id = _require_str(params, "target_id")
        max_depth = params.get("max_depth", 5)
        if not isinstance(max_depth, int) or max_depth < 0:
            raise _OpError("'max_depth' must be a non-negative integer.")
        return {
            "result": _path_between(
                blob,
                source_id,
                target_id,
                max_depth=max_depth,
            ),
        }

    if op == "violates_boundaries":
        source_id = _require_str(params, "source_id")
        target_id = _require_str(params, "target_id")
        edge = _violates_boundaries(blob, source_id, target_id)
        return {"result": edge.model_dump(mode="json") if edge else None}

    # Defensive — execute() validated op, but keep the unreachable branch.
    raise _OpError(f"unknown op '{op}'")


def _require_str(params: dict[str, Any], key: str) -> str:
    """Pull ``key`` out of ``params`` as a non-empty str or raise."""
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise _OpError(f"'{key}' is required and must be a non-empty string.")
    return value


# ---------------------------------------------------------------------------
# Per-op handlers (pure over the blob)
# ---------------------------------------------------------------------------


def _callers_of(blob: RepoGraphBlob, node_id: str) -> list[Node]:
    """Nodes that have at least one edge pointing AT ``node_id``."""
    by_id = {n.id: n for n in blob.nodes}
    sources: list[Node] = []
    seen: set[str] = set()
    for edge in blob.edges:
        if edge.target != node_id:
            continue
        if edge.source in seen:
            continue
        seen.add(edge.source)
        n = by_id.get(edge.source)
        if n is not None:
            sources.append(n)
    return sources


def _callees_of(blob: RepoGraphBlob, node_id: str) -> list[Node]:
    """Nodes that ``node_id`` has at least one edge pointing AT."""
    by_id = {n.id: n for n in blob.nodes}
    targets: list[Node] = []
    seen: set[str] = set()
    for edge in blob.edges:
        if edge.source != node_id:
            continue
        if edge.target in seen:
            continue
        seen.add(edge.target)
        n = by_id.get(edge.target)
        if n is not None:
            targets.append(n)
    return targets


def _outgoing_edges(blob: RepoGraphBlob, node_id: str) -> list[Edge]:
    return [e for e in blob.edges if e.source == node_id]


def _incoming_edges(blob: RepoGraphBlob, node_id: str) -> list[Edge]:
    return [e for e in blob.edges if e.target == node_id]


def _public_surface(blob: RepoGraphBlob, area_name: str) -> list[Node]:
    """Nodes in ``area_name`` whose id is in ``blob.public_symbols``.

    The set comes from the pipeline's per-area union of parser-declared
    public symbols (Phase 5; surfaced on the blob in Phase 6).
    """
    public = set(blob.public_symbols)
    return [n for n in blob.nodes if n.area == area_name and n.id in public]


def _path_between(
    blob: RepoGraphBlob,
    source_id: str,
    target_id: str,
    *,
    max_depth: int,
) -> list[str]:
    """BFS shortest path source -> target by node id.

    Returns ``[source_id, ..., target_id]`` when a path exists within
    ``max_depth`` edges, otherwise ``[]``. ``source_id == target_id``
    returns the single-element path ``[source_id]`` when the node
    exists in the graph (consistent with "the node trivially reaches
    itself in zero hops"). A non-existent source returns ``[]``.
    """
    node_ids = {n.id for n in blob.nodes}
    if source_id not in node_ids:
        return []
    if source_id == target_id:
        return [source_id]
    if target_id not in node_ids:
        # Edges might still reference an absent target id, but the
        # convention for path_between is that both endpoints exist.
        return []

    # Adjacency built once — O(E).
    adj: dict[str, list[str]] = {}
    for edge in blob.edges:
        adj.setdefault(edge.source, []).append(edge.target)

    # BFS with depth tracking; back-pointers reconstruct the path.
    parents: dict[str, str | None] = {source_id: None}
    depths: dict[str, int] = {source_id: 0}
    queue: deque[str] = deque([source_id])
    while queue:
        current = queue.popleft()
        depth = depths[current]
        if depth >= max_depth:
            continue
        for nxt in adj.get(current, ()):
            if nxt in parents:
                continue
            parents[nxt] = current
            depths[nxt] = depth + 1
            if nxt == target_id:
                return _reconstruct_path(parents, target_id)
            queue.append(nxt)
    return []


def _reconstruct_path(
    parents: dict[str, str | None],
    target_id: str,
) -> list[str]:
    """Walk back through ``parents`` from ``target_id`` to the root."""
    path: list[str] = []
    cur: str | None = target_id
    while cur is not None:
        path.append(cur)
        cur = parents.get(cur)
    path.reverse()
    return path


def _violates_boundaries(
    blob: RepoGraphBlob,
    source_id: str,
    target_id: str,
) -> Edge | None:
    """Return the first edge from ``source_id`` to ``target_id``, or None.

    The caller reads ``edge.boundary_violation`` and ``violation_reason``
    directly — Phase 5 already populated those fields on every edge.
    """
    for edge in blob.edges:
        if edge.source == source_id and edge.target == target_id:
            return edge
    return None


# ---------------------------------------------------------------------------
# Existence flagging — workspace file presence
# ---------------------------------------------------------------------------


def _wrap_nodes(
    nodes: list[Node],
    workspace_path: str,
) -> dict[str, Any]:
    """Wrap a list of nodes with ``exists_in_workspace`` flags."""
    items = [
        {
            "value": n.model_dump(mode="json"),
            "exists_in_workspace": _node_exists(n, workspace_path),
        }
        for n in nodes
    ]
    return {
        "result": [n.model_dump(mode="json") for n in nodes],
        "results_with_existence": items,
    }


def _wrap_edges(
    edges: list[Edge],
    blob: RepoGraphBlob,
    workspace_path: str,
) -> dict[str, Any]:
    """Wrap a list of edges with per-edge ``exists_in_workspace``.

    An edge "exists" if both its source and target node files exist in
    the workspace. Area-kind / file-less endpoints are treated as
    existing (vacuous truth). This is the conservative choice — the
    agent only filters out edges whose grounding files clearly disappeared.
    """
    by_id = {n.id: n for n in blob.nodes}
    items = []
    for e in edges:
        src = by_id.get(e.source)
        tgt = by_id.get(e.target)
        existed = (_node_exists(src, workspace_path) if src is not None else False) and (
            _node_exists(tgt, workspace_path) if tgt is not None else False
        )
        items.append(
            {
                "value": e.model_dump(mode="json"),
                "exists_in_workspace": bool(existed),
            },
        )
    return {
        "result": [e.model_dump(mode="json") for e in edges],
        "results_with_existence": items,
    }


def _node_exists(node: Node, workspace_path: str) -> bool:
    """True iff the node's source file exists under ``workspace_path``.

    Nodes without a ``file`` (area-kind) are vacuously existing — there
    is no on-disk artifact to check for. An empty workspace_path is
    treated as "we don't have an analyser workspace on this host" → all
    file-bearing nodes are reported missing so the agent doesn't trust
    them.
    """
    if node.file is None:
        return True
    if not workspace_path:
        return False
    full = os.path.join(workspace_path, node.file)
    return os.path.exists(full)


__all__ = ["QueryRepoGraphTool"]
