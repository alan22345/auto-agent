"""End-to-end pipeline for ADR-016 Phases 2 + 3.

Stages (matches ADR §10):

1. Resolve area layout — read ``.auto-agent/graph.yml`` if present,
   else default to top-level directories (with a stable skip-list).
2. For each area: walk files, dispatch by extension to the right parser
   (via :func:`agent.graph_analyzer.parsers.parser_for`), accumulate
   nodes, AST edges, and unresolved-dispatch sites.
3. **Phase 3 gap-fill** — per area, when an ``LLMProvider`` is
   supplied: feed each :class:`UnresolvedSite` through
   :func:`gap_fill_site`; fall back to :func:`agent_escape` when the
   one-shot returns zero validatable edges. Every emitted edge is
   then run through the unconditional citation + target validators
   (``agent/graph_analyzer/validator.py``); failures are dropped.
4. Per-area failure isolation — a parser exception marks the area
   ``failed`` and continues with the next area. Gap-fill exceptions
   for a single site never fail the area.
5. Assemble the :class:`shared.types.RepoGraphBlob`. Overall status:
   ``ok`` if every area succeeded, ``partial`` if some did, ``failed``
   if none did.

When ``provider=None`` the pipeline behaves exactly as in Phase 2 —
AST-only output, no LLM call.
"""

from __future__ import annotations

import fnmatch
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
import yaml

from agent.graph_analyzer.agent_escape import agent_escape
from agent.graph_analyzer.boundaries import flag_violations, load_boundary_rules
from agent.graph_analyzer.gap_fill import gap_fill_site
from agent.graph_analyzer.http_match import match_http_edges
from agent.graph_analyzer.parsers import parser_for, supported_extensions
from agent.graph_analyzer.validator import validate_citation, validate_target
from shared.types import AreaStatus, Edge, Node, RepoGraphBlob

if TYPE_CHECKING:
    from agent.graph_analyzer.parsers import ParseResult
    from agent.graph_analyzer.types import UnresolvedSite
    from agent.llm.base import LLMProvider

log = structlog.get_logger(__name__)

# Bumped per phase as new capability lands. Phase 5 adds cross-area
# boundary-violation flagging (ADR-016 §7) on top of Phase 4's HTTP
# matching. Even when ``provider=None`` is passed the analyser version
# still records that the binary is *capable* of LLM gap-fill — useful
# for downstream consumers to tell graphs apart across phases.
_ANALYSER_VERSION = "phase5-multi-0.5.0"

# Directories always excluded from area discovery. Matches the spec —
# tests/ is deliberately *not* in here (analyse it if it's a top-level
# directory; the user can exclude it via graph.yml).
_DEFAULT_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".next",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".auto-agent",
    }
)

# Per-file skip — never analyse migrations/versions/* (Alembic-style
# revision files churn and don't reflect the real architecture).
_FILE_SKIP_PATH_FRAGMENTS: tuple[str, ...] = ("migrations/versions/",)


def analyser_version() -> str:
    """Public accessor for the analyser version string."""
    return _ANALYSER_VERSION


# ----------------------------------------------------------------------
# Area discovery
# ----------------------------------------------------------------------


def _discover_areas(workspace: str) -> list[tuple[str, list[str]]]:
    """Return ``[(area_name, [glob_pattern, ...]), ...]`` for the workspace.

    Reads ``.auto-agent/graph.yml`` if present with shape::

        areas:
          - name: backend
            paths: ["src/api/**", "src/services/**"]

    Otherwise defaults to one area per top-level directory (skipping the
    default exclude list).
    """
    yml = os.path.join(workspace, ".auto-agent", "graph.yml")
    if os.path.isfile(yml):
        try:
            with open(yml) as f:
                data = yaml.safe_load(f) or {}
            raw_areas = data.get("areas") or []
            areas: list[tuple[str, list[str]]] = []
            for entry in raw_areas:
                name = entry.get("name")
                paths = entry.get("paths") or []
                if isinstance(name, str) and name:
                    areas.append((name, [str(p) for p in paths]))
            if areas:
                return areas
        except Exception as e:
            log.warning(
                "graph_yml_parse_failed",
                workspace=workspace,
                error=str(e),
            )

    # Default — top-level directories.
    discovered: list[tuple[str, list[str]]] = []
    for entry in sorted(os.listdir(workspace)):
        full = os.path.join(workspace, entry)
        if not os.path.isdir(full):
            continue
        if entry in _DEFAULT_EXCLUDE_DIRS:
            continue
        discovered.append((entry, [f"{entry}/**"]))
    return discovered


def _iter_area_files(workspace: str, patterns: list[str]) -> list[str]:
    """Yield workspace-relative file paths matching ``patterns``.

    Uses :mod:`fnmatch` semantics — patterns are matched against the
    workspace-relative path with forward slashes. The default-excluded
    directories are filtered at walk time so we never recurse into a
    100 MB ``node_modules``.
    """
    out: list[str] = []
    ws_abs = os.path.abspath(workspace)
    exts = supported_extensions()
    for dirpath, dirnames, filenames in os.walk(ws_abs):
        # In-place filter to skip the deep dirs.
        dirnames[:] = [d for d in dirnames if d not in _DEFAULT_EXCLUDE_DIRS]
        rel_dir = os.path.relpath(dirpath, ws_abs)
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext not in exts:
                continue
            rel = f if rel_dir == "." else f"{rel_dir}/{f}"
            rel = rel.replace(os.sep, "/")
            if any(frag in rel for frag in _FILE_SKIP_PATH_FRAGMENTS):
                continue
            if _matches_any(rel, patterns):
                out.append(rel)
    out.sort()
    return out


def _matches_any(rel: str, patterns: list[str]) -> bool:
    """Match ``rel`` against any of the ``patterns``.

    Patterns ending in ``/**`` match anything inside that directory or
    the directory itself; ``fnmatch`` doesn't handle ``**`` natively, so
    we expand it.
    """
    for pat in patterns:
        if pat.endswith("/**"):
            base = pat[:-3]
            if rel == base or rel.startswith(base + "/"):
                return True
        elif fnmatch.fnmatchcase(rel, pat):
            return True
    return False


# ----------------------------------------------------------------------
# Per-area analysis
# ----------------------------------------------------------------------


def _analyse_area(
    *,
    workspace: str,
    area_name: str,
    patterns: list[str],
) -> tuple[list[Node], list[Edge], list[UnresolvedSite], set[str], AreaStatus]:
    """Run the parser dispatch over one area.

    Returns the area's nodes, AST edges, unresolved sites, the union of
    public-symbol ids contributed by every parsed file (Phase 5 — used
    by the boundary-flagging stage), and an :class:`AreaStatus` (``ok``
    on success; ``failed`` if a parser exception bubbled up). Individual
    files the parser handled gracefully (e.g. tree-sitter ERROR-node
    recovery) do NOT fail the area — only an unhandled exception does.

    Unresolved sites are returned to the pipeline so the gap-fill
    stage can run against them. ``AreaStatus.unresolved_dynamic_sites``
    is the count of those sites.
    """
    nodes: list[Node] = []
    edges: list[Edge] = []
    unresolved_sites: list[UnresolvedSite] = []
    public_symbols: set[str] = set()

    # Area node — every area gets one root compound box.
    nodes.append(
        Node(
            id=f"area:{area_name}",
            kind="area",
            label=area_name,
            file=None,
            line_start=None,
            line_end=None,
            area=area_name,
            parent=None,
        ),
    )

    files = _iter_area_files(workspace, patterns)
    try:
        for rel in files:
            parser = parser_for(rel)
            if parser is None:
                continue
            abs_path = os.path.join(workspace, rel)
            try:
                with open(abs_path, "rb") as f:
                    source = f.read()
            except OSError as e:
                log.warning(
                    "graph_file_read_failed",
                    file=rel,
                    error=str(e),
                )
                continue
            try:
                pr: ParseResult = parser.parse_file(
                    rel_path=rel,
                    area=area_name,
                    source=source,
                )
            except Exception as e:
                log.warning(
                    "graph_file_parse_failed",
                    file=rel,
                    error=str(e),
                )
                # A *single* file's parser exception doesn't fail the
                # area — only an exception that escapes the per-file
                # try/except (i.e. our own logic broke) does.
                continue
            nodes.extend(pr.nodes)
            edges.extend(pr.edges)
            unresolved_sites.extend(pr.unresolved_sites)
            public_symbols.update(pr.public_symbols)
    except Exception as e:
        log.exception("graph_area_failed", area=area_name, error=str(e))
        return (
            nodes,
            edges,
            unresolved_sites,
            public_symbols,
            AreaStatus(
                name=area_name,
                status="failed",
                error=str(e) or e.__class__.__name__,
                unresolved_dynamic_sites=len(unresolved_sites),
            ),
        )

    return (
        nodes,
        edges,
        unresolved_sites,
        public_symbols,
        AreaStatus(
            name=area_name,
            status="ok",
            error=None,
            unresolved_dynamic_sites=len(unresolved_sites),
        ),
    )


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


async def run_pipeline(
    *,
    workspace: str,
    commit_sha: str,
    provider: LLMProvider | None = None,
) -> RepoGraphBlob:
    """Run the full Phase 2 + Phase 3 pipeline against ``workspace``.

    Args:
        workspace: Absolute path to a prepared workspace. The caller
            owns clone/fetch/reset; the pipeline only reads.
        commit_sha: Resolved HEAD sha for the blob's record.
        provider: Optional LLM provider. When ``None``, the pipeline
            emits AST-only edges (Phase 2 behaviour). When provided,
            each :class:`UnresolvedSite` runs through one-shot gap-fill
            and (on empty/invalid result) the bounded agent-escape;
            validated LLM edges land in the blob with
            ``source_kind="llm"``.
    """
    areas = _discover_areas(workspace)

    all_nodes: list[Node] = []
    all_edges: list[Edge] = []
    statuses: list[AreaStatus] = []
    # Per-area unresolved sites are kept aside so we can run gap-fill
    # only after every area's nodes are known (gives a richer candidate
    # pool to draw from than within-area-only).
    per_area_sites: list[tuple[str, list[UnresolvedSite]]] = []
    # Union of per-area public-surface symbol ids. The Phase 5 boundary
    # check reads this to decide whether a cross-area edge's target is
    # part of the destination area's public surface.
    all_public_symbols: set[str] = set()

    for name, patterns in areas:
        nodes, edges, sites, public_symbols, status = _analyse_area(
            workspace=workspace,
            area_name=name,
            patterns=patterns,
        )
        all_nodes.extend(nodes)
        all_edges.extend(edges)
        per_area_sites.append((name, sites))
        all_public_symbols.update(public_symbols)
        statuses.append(status)

    # Gap-fill stage. Skipped when no provider is supplied.
    if provider is not None:
        llm_edges = await _run_gap_fill_stage(
            workspace=workspace,
            provider=provider,
            nodes=all_nodes,
            per_area_sites=per_area_sites,
        )
        all_edges.extend(llm_edges)

    # Cross-language HTTP matching (Phase 4). Runs after all per-area
    # parsing + gap-fill because route discovery needs the full
    # node list (including decorator metadata) and frontend HTTP
    # calls need the full set of backend routes as candidates.
    # Failures are isolated inside ``match_http_edges`` itself; we
    # still defend against an outer exception here so the pipeline
    # always assembles a blob.
    try:
        http_edges = await match_http_edges(
            workspace_path=workspace,
            nodes=all_nodes,
            provider=provider,
        )
        all_edges.extend(http_edges)
    except Exception as e:
        log.warning(
            "graph_http_match_stage_failed",
            error=str(e),
            error_type=e.__class__.__name__,
        )

    # Phase 5 (ADR-016 §7) — boundary-violation flagging.
    #
    # First, rewrite cross-area edges whose target is still a ``module:``
    # placeholder to the actual file-level symbol id when one exists in
    # the graph. The parser binds ``from area_b.public_api import x`` to
    # ``module:area_b.public_api.x`` because per-file parsing has no
    # awareness of other files; the pipeline is the first place that can
    # resolve those references against the full node set. We only
    # rewrite cross-area edges so that intra-area module: edges (which
    # carry no boundary semantics) stay shaped the way Phase 2 callers
    # expect.
    all_edges = _resolve_cross_area_module_targets(all_edges, all_nodes)

    # Then, run the boundary flagger. It mutates no inputs — returns a
    # new edge list with ``boundary_violation`` and ``violation_reason``
    # populated. HTTP edges are unconditionally exempt; same-area edges
    # are exempt; edges into area / file nodes are exempt.
    rules = load_boundary_rules(workspace)
    all_edges = flag_violations(
        edges=all_edges,
        nodes=all_nodes,
        public_symbols=all_public_symbols,
        rules=rules,
    )

    return RepoGraphBlob(
        commit_sha=commit_sha,
        generated_at=datetime.now(UTC),
        analyser_version=_ANALYSER_VERSION,
        areas=statuses,
        nodes=all_nodes,
        edges=all_edges,
        public_symbols=sorted(all_public_symbols),
    )


def _resolve_cross_area_module_targets(
    edges: list[Edge],
    nodes: list[Node],
) -> list[Edge]:
    """Rewrite ``module:<module>.<symbol>`` targets to ``<file>::<symbol>``
    when (a) a matching node exists and (b) the resolved target's area
    differs from the source's area.

    Same-area edges keep their original ``module:`` target — Phase 2
    callers (and existing tests) depend on that shape, and same-area
    edges never feed the boundary check anyway.

    Edges whose source has no matching node, whose target is not a
    ``module:<dotted>.<symbol>`` shape, or whose computed resolution
    has no matching node, are passed through unchanged.
    """
    # Build a lookup from synthetic ``module:`` ids to the real
    # ``<file>::<symbol>`` ids for every class/function node in the graph.
    # We compute the module form once per node by walking back from the
    # node's file path.
    module_lookup: dict[str, Node] = {}
    for n in nodes:
        if n.kind not in ("class", "function"):
            continue
        if not n.file:
            continue
        # Only file-level symbols (no nesting like ``Foo.method`` or
        # ``outer.inner``) can be addressed via ``module:pkg.mod.symbol``.
        # Nested function ids embed dots after ``::``; skip those.
        suffix = n.id.split("::", 1)[-1]
        if "." in suffix:
            continue
        module_dotted = _file_to_module(n.file)
        if module_dotted is None:
            continue
        module_lookup[f"module:{module_dotted}.{suffix}"] = n

    node_by_id: dict[str, Node] = {n.id: n for n in nodes}

    rewritten: list[Edge] = []
    for edge in edges:
        if not edge.target.startswith("module:"):
            rewritten.append(edge)
            continue
        resolved = module_lookup.get(edge.target)
        if resolved is None:
            rewritten.append(edge)
            continue
        source_node = node_by_id.get(edge.source)
        if source_node is None or source_node.area == resolved.area:
            # Same-area or unknown-source — leave target unchanged.
            rewritten.append(edge)
            continue
        rewritten.append(
            Edge(
                source=edge.source,
                target=resolved.id,
                kind=edge.kind,
                evidence=edge.evidence,
                source_kind=edge.source_kind,
                boundary_violation=edge.boundary_violation,
                violation_reason=edge.violation_reason,
            ),
        )
    return rewritten


def _file_to_module(file_path: str) -> str | None:
    """Convert a workspace-relative file path into its dotted module form.

    ``a/b/c.py`` → ``a.b.c``. ``a/b/__init__.py`` → ``a.b``.
    TypeScript files (``.ts`` / ``.tsx``) are converted the same way
    (``frontend/mod.ts`` → ``frontend.mod``) — the parser uses the same
    encoding when emitting synthetic ``module:`` ids.

    Returns ``None`` for files with no recognised extension; callers
    then skip the lookup for that node.
    """
    for ext in (".py", ".tsx", ".ts", ".jsx", ".js"):
        if file_path.endswith(ext):
            stem = file_path[: -len(ext)]
            parts = stem.split("/")
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            return ".".join(parts)
    return None


async def _run_gap_fill_stage(
    *,
    workspace: str,
    provider: LLMProvider,
    nodes: list[Node],
    per_area_sites: list[tuple[str, list[UnresolvedSite]]],
) -> list[Edge]:
    """For each unresolved site, run gap-fill (and on empty/invalid
    result, agent-escape). Validate every emitted edge against the
    actual workspace; drop failures. Returns the surviving LLM edges.

    Validation order:
      1. Soft filter inside gap-fill / escape: target in candidate pool.
      2. ``validate_citation`` against the workspace.
      3. ``validate_target`` against the final node set.

    Step 1 is a fast belt-and-braces check; steps 2 + 3 are the
    unconditional gates promised by ADR-016 §3.
    """
    validated: list[Edge] = []
    for area_name, sites in per_area_sites:
        candidates = _candidate_pool_for_area(nodes, area_name)
        for site in sites:
            try:
                llm_edges = await gap_fill_site(
                    provider=provider,
                    workspace_path=workspace,
                    site=site,
                    candidate_nodes=candidates,
                )
            except Exception as e:
                log.warning(
                    "graph_gap_fill_site_unexpected",
                    site=site.containing_node_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                )
                llm_edges = []

            # Escalate to bounded agent-loop if one-shot was empty OR if
            # every emitted edge fails the validation gates.
            survivors = _validate_edges(workspace, llm_edges, nodes)
            if not survivors:
                try:
                    escape_edges = await agent_escape(
                        provider=provider,
                        workspace_path=workspace,
                        site=site,
                        candidate_nodes=candidates,
                    )
                except Exception as e:
                    log.warning(
                        "graph_agent_escape_site_unexpected",
                        site=site.containing_node_id,
                        error=str(e),
                        error_type=e.__class__.__name__,
                    )
                    escape_edges = []
                survivors = _validate_edges(workspace, escape_edges, nodes)

            validated.extend(survivors)
    return validated


def _validate_edges(
    workspace: str,
    edges: list[Edge],
    nodes: list[Node],
) -> list[Edge]:
    """Apply ``validate_citation`` and ``validate_target`` to ``edges``.

    Both checks must pass for an edge to survive. The function never
    raises — drops are logged inside the validator helpers.
    """
    out: list[Edge] = []
    for edge in edges:
        if not validate_citation(workspace, edge):
            continue
        if not validate_target(edge, nodes):
            continue
        out.append(edge)
    return out


def _candidate_pool_for_area(
    nodes: list[Node],
    area_name: str,
) -> list[Node]:
    """Order ``nodes`` by likely relevance to ``area_name``.

    Priority: nodes inside this area first, then nodes from any other
    area. The downstream gap-fill / escape modules apply their own
    150-node cap, so this is purely an *ordering* concern — we never
    drop nodes here.

    Phase 5 will introduce the proper "visible to this file via imports"
    filter. For Phase 3 the area-first ordering is enough.
    """
    in_area = [n for n in nodes if n.area == area_name]
    out_area = [n for n in nodes if n.area != area_name]
    return in_area + out_area


def overall_status(statuses: list[AreaStatus]) -> str:
    """Compute the overall ``RepoGraph.status`` from per-area outcomes.

    Returns one of ``"ok" | "partial" | "failed"`` per ADR-016 §10:

    * ``"ok"`` — every area succeeded.
    * ``"partial"`` — at least one ok and at least one failed.
    * ``"failed"`` — every area failed (or no areas were discovered).
    """
    if not statuses:
        return "failed"
    n_failed = sum(1 for s in statuses if s.status == "failed")
    if n_failed == 0:
        return "ok"
    if n_failed == len(statuses):
        return "failed"
    return "partial"
