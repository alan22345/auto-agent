"""End-to-end pipeline for ADR-016 Phases 2 + 3.

Stages (matches ADR §10):

1. Resolve area layout — read ``.auto-agent/graph.yml`` if present,
   else default to top-level directories (with a stable skip-list).
2. For each area: walk files, dispatch by extension to the right parser
   (via :func:`agent.graph_analyzer.parsers.parser_for`), accumulate
   nodes, AST edges, and unresolved-dispatch sites.
3. **Gap-fill** — when an ``LLMProvider`` is supplied: each
   :class:`UnresolvedSite` runs through a single one-shot
   :func:`gap_fill_site` call. Sites are dispatched concurrently
   (bounded by :data:`_GAP_FILL_CONCURRENCY`); every emitted edge runs
   through the unconditional citation + target validators
   (``agent/graph_analyzer/validator.py``); failures are dropped. There
   is no multi-turn agent-escape fallback — empty one-shot results
   simply yield no LLM edge for that site (the redesign that replaced
   the 27-42h cardamon stall with a few minutes).
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

import asyncio
import fnmatch
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
import yaml

from agent.graph_analyzer.boundaries import flag_violations, load_boundary_rules
from agent.graph_analyzer.cycles import compute_cycles
from agent.graph_analyzer.gap_fill import gap_fill_site
from agent.graph_analyzer.http_match import match_http_edges
from agent.graph_analyzer.parsers import parser_for, supported_extensions
from agent.graph_analyzer.test_filter import is_test_file
from agent.graph_analyzer.validator import validate_citation, validate_target
from shared.types import AreaStatus, Edge, Node, RepoGraphBlob

if TYPE_CHECKING:
    from agent.graph_analyzer.parsers import ParseResult
    from agent.graph_analyzer.types import UnresolvedSite
    from agent.llm.base import LLMProvider

log = structlog.get_logger(__name__)

CheckpointFlush = Callable[[dict, dict, list], Awaitable[None]]
# (blob_dict, processed_files_dict, failed_sites_list) -> None

#: Type alias for the live-progress hook. Callers (the refresh
#: lifecycle handler) pass one in to receive a ``(done, total)`` ping
#: per completed gap-fill site. The pipeline never imports the
#: orchestrator concern (in-memory tracker, event bus); this seam
#: keeps the dependency arrow correct.
GapFillProgressHook = Callable[[int, int], Awaitable[None]]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


#: Concurrency cap for gap-fill across sites. Each site is one cheap
#: Haiku call (~5s), independent of the others. 8-in-flight is the
#: knee where additional parallelism stops translating to wall-clock
#: gains given Bedrock's per-account throttling.
_GAP_FILL_CONCURRENCY = 8


# Bumped per phase as new capability lands. Phase 7 adds partial-mode
# (per-area) refresh on top of Phase 5/6. Even when ``provider=None``
# is passed the analyser version still records that the binary is
# *capable* of LLM gap-fill — useful for downstream consumers to tell
# graphs apart across phases. Phase 8 adds per-function complexity fields
# (cyclomatic/cognitive/loc) to Node. Phase 9 adds import-cycle detection
# (Tarjan SCC over imports edges) and the DependencyCycle schema.
# Phase 10 adds dead-code findings (DeadCodeFinding schema + dead_code field).
_ANALYSER_VERSION = "phase10-deadcode-0.10.0"

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


def walk_files(workspace: str) -> list[str]:
    """Return all workspace-relative source file paths eligible for analysis.

    Applies the standard exclusions:

    * Default-excluded directories (``node_modules``, ``.git``, etc.) are
      never recursed into.
    * Files matching :data:`_FILE_SKIP_PATH_FRAGMENTS` (e.g. Alembic
      migration revisions) are skipped.
    * Test / spec / fixture files are skipped via
      :func:`~agent.graph_analyzer.test_filter.is_test_file`.

    The returned list is sorted for determinism. Only files with extensions
    in :func:`~agent.graph_analyzer.parsers.supported_extensions` are
    included.
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
            if is_test_file(rel):
                continue
            out.append(rel)
    out.sort()
    return out


def _iter_area_files(workspace: str, patterns: list[str]) -> list[str]:
    """Return workspace-relative file paths matching ``patterns``.

    Uses :mod:`fnmatch` semantics — patterns are matched against the
    workspace-relative path with forward slashes. Delegates the file walk
    to :func:`walk_files` so that exclusion logic is centralised.
    """
    all_files = walk_files(workspace)
    return sorted(rel for rel in all_files if _matches_any(rel, patterns))


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


async def _analyse_area(
    *,
    workspace: str,
    area_name: str,
    patterns: list[str],
    blob_dict: dict | None = None,
    processed_files: dict | None = None,
    failed_sites: list | None = None,
    on_file_checkpoint: CheckpointFlush | None = None,
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

    When ``on_file_checkpoint`` is provided, it is called after each file
    completes. ``blob_dict``, ``processed_files``, and ``failed_sites`` are
    mutated in-place and passed to the callback so the caller can persist
    incremental progress.
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
            # Skip-if-already-processed gate (checkpoint/resume support).
            # A file is retried if it previously appeared in failed_sites.
            if processed_files is not None and failed_sites is not None:
                retry_due = any(s.get("file") == rel for s in failed_sites)
                if rel in processed_files and not retry_due:
                    continue

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
                if processed_files is not None and failed_sites is not None:
                    # Record as a failed site so the caller can retry later.
                    failed_sites[:] = [s for s in failed_sites if s.get("file") != rel]
                    failed_sites.append({"file": rel, "reason": "read_error", "error": str(e)})
                    if on_file_checkpoint is not None and blob_dict is not None:
                        await on_file_checkpoint(blob_dict, processed_files, failed_sites)
                continue
            file_failed_sites: list = []
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
                if processed_files is not None and failed_sites is not None:
                    failed_sites[:] = [s for s in failed_sites if s.get("file") != rel]
                    failed_sites.append({"file": rel, "reason": "parse_error", "error": str(e)})
                    if on_file_checkpoint is not None and blob_dict is not None:
                        await on_file_checkpoint(blob_dict, processed_files, failed_sites)
                continue
            nodes.extend(pr.nodes)
            edges.extend(pr.edges)
            unresolved_sites.extend(pr.unresolved_sites)
            public_symbols.update(pr.public_symbols)

            # Update checkpoint state for this file.
            if processed_files is not None and failed_sites is not None:
                if blob_dict is not None:
                    blob_dict["nodes"].extend(
                        [n.model_dump() if hasattr(n, "model_dump") else n for n in pr.nodes]
                    )
                    blob_dict["edges"].extend(
                        [e.model_dump() if hasattr(e, "model_dump") else e for e in pr.edges]
                    )
                # Clear any previous failure record for this file.
                failed_sites[:] = [
                    s for s in failed_sites if s.get("file") != rel
                ] + file_failed_sites
                processed_files[rel] = {
                    "sites_attempted": len(pr.unresolved_sites),
                    "sites_succeeded": len(pr.unresolved_sites),
                    "edges_added": len(pr.edges),
                    "processed_at": _now_iso(),
                }
                if on_file_checkpoint is not None and blob_dict is not None:
                    await on_file_checkpoint(blob_dict, processed_files, failed_sites)
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
    on_file_checkpoint: CheckpointFlush | None = None,
    on_progress: GapFillProgressHook | None = None,
    initial_processed_files: dict | None = None,
    initial_failed_sites: list | None = None,
    initial_blob: dict | None = None,
) -> RepoGraphBlob:
    """Run the full Phase 2 + Phase 3 pipeline against ``workspace``.

    Args:
        workspace: Absolute path to a prepared workspace. The caller
            owns clone/fetch/reset; the pipeline only reads.
        commit_sha: Resolved HEAD sha for the blob's record.
        provider: Optional LLM provider. When ``None``, the pipeline
            emits AST-only edges (Phase 2 behaviour). When provided,
            each :class:`UnresolvedSite` runs through one-shot gap-fill
            and validated LLM edges land in the blob with
            ``source_kind="llm"`` (no multi-turn fallback).
        on_file_checkpoint: Optional async callback invoked after each
            file completes. Receives ``(blob_dict, processed_files,
            failed_sites)`` so the caller can persist incremental
            progress (e.g. for crash recovery). When ``None``, the
            pipeline behaves exactly as before.
        on_progress: Optional async callback ``(done, total) -> None``.
            Fires once per completed gap-fill site so callers can
            surface live "X of Y" progress to the UI. The pipeline
            never reads/writes the orchestrator-side tracker itself —
            the callback is the seam.
        initial_processed_files: Resume state — dict of already-processed
            file paths to their metadata. Files present here are skipped
            unless they also appear in ``initial_failed_sites``.
        initial_failed_sites: Resume state — list of dicts describing
            files that failed in a previous run and should be retried.
        initial_blob: Resume state — partial blob dict to extend. When
            provided, its ``nodes`` and ``edges`` are preserved.
    """
    areas = _discover_areas(workspace)

    # Initialise resume/checkpoint state.
    processed_files: dict | None = None
    failed_sites: list | None = None
    blob_dict: dict | None = None

    if on_file_checkpoint is not None:
        processed_files = dict(initial_processed_files or {})
        failed_sites = list(initial_failed_sites or [])
        if initial_blob is not None:
            blob_dict = dict(initial_blob)
            blob_dict.setdefault("nodes", [])
            blob_dict.setdefault("edges", [])
        else:
            blob_dict = {
                "nodes": [],
                "edges": [],
                "commit_sha": commit_sha,
                "areas": [],
                "public_symbols": [],
            }

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

    # Resume: seed the accumulators from inherited checkpoint state so
    # files we skip in the area loop still contribute to the returned
    # blob — and so post-processing stages (gap-fill, http_match,
    # boundary) see the full graph. Without this, the pipeline's return
    # value drops everything from previously-processed files and the
    # caller's final overwrite of ``r.graph_json`` destroys 594 files of
    # accumulated work.
    if initial_blob is not None and on_file_checkpoint is not None:
        for n in initial_blob.get("nodes", []) or []:
            all_nodes.append(n if isinstance(n, Node) else Node.model_validate(n))
        for e in initial_blob.get("edges", []) or []:
            all_edges.append(e if isinstance(e, Edge) else Edge.model_validate(e))
        all_public_symbols.update(initial_blob.get("public_symbols") or [])

    for name, patterns in areas:
        nodes, edges, sites, public_symbols, status = await _analyse_area(
            workspace=workspace,
            area_name=name,
            patterns=patterns,
            blob_dict=blob_dict,
            processed_files=processed_files,
            failed_sites=failed_sites,
            on_file_checkpoint=on_file_checkpoint,
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
            on_progress=on_progress,
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

    # Resolve bare ``module:<dotted>`` endpoints on import edges to the
    # corresponding ``file:`` node id, and drop edges whose endpoints
    # still don't resolve to a real node. Per-file parsers emit import
    # edges between two ``module:`` placeholders because they have no
    # cross-file knowledge; without this step the rendered graph carries
    # phantom endpoints that cytoscape silently drops (and breaks its
    # layout, leaving every node at the origin).
    all_edges = _resolve_module_imports_to_files(all_edges, all_nodes)

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
        cycles=compute_cycles(all_edges),
    )


async def run_partial_pipeline(
    *,
    workspace: str,
    commit_sha: str,
    target_area: str,
    previous_blob: RepoGraphBlob,
    provider: LLMProvider | None = None,
    on_progress: GapFillProgressHook | None = None,
) -> RepoGraphBlob:
    """Re-analyse a single area and splice the result into the previous
    blob (ADR-016 §10 — Phase 7).

    Contract:

    * Only ``target_area``'s files are re-parsed. Nodes and edges from
      every other area are preserved verbatim from ``previous_blob``.
    * HTTP matching re-runs across the full node set so a new route in
      the target area can match an inherited frontend call.
    * Cross-area boundary flagging re-runs across the full edge set so
      changes in the target area's public surface re-validate inherited
      cross-area edges.
    * Failure isolation: a parser exception inside the target area
      records ``AreaStatus.status="failed"`` for that area, but never
      drops the surviving inherited data.
    * When ``target_area`` is not present in the workspace's discovered
      area list AND not in ``previous_blob.areas``, the blob records an
      ``AreaStatus(name=target_area, status="failed", error="unknown
      area")``. The pipeline still produces a complete blob.

    The output is otherwise indistinguishable from a full refresh
    against the same workspace state — same node ids, same edge keys,
    same boundary flags.
    """
    discovered = _discover_areas(workspace)
    discovered_by_name = dict(discovered)

    # ------------------------------------------------------------------
    # 1. Inherited data — everything *not* in the target area.
    # ------------------------------------------------------------------
    inherited_nodes: list[Node] = [n for n in previous_blob.nodes if n.area != target_area]
    inherited_node_ids = {n.id for n in inherited_nodes}
    # Inherited edges = those whose source node is preserved. Edges
    # whose source lives inside the target area are dropped because the
    # target area is being fully re-parsed.
    previous_node_areas = {n.id: n.area for n in previous_blob.nodes}
    inherited_edges: list[Edge] = [
        e
        for e in previous_blob.edges
        if previous_node_areas.get(e.source, target_area) != target_area
    ]
    inherited_public_symbols = {
        s
        for s in previous_blob.public_symbols
        if previous_node_areas.get(s, target_area) != target_area
    }
    inherited_statuses = [a for a in previous_blob.areas if a.name != target_area]

    # ------------------------------------------------------------------
    # 2. Re-analyse the target area (when discoverable).
    # ------------------------------------------------------------------
    target_nodes: list[Node] = []
    target_edges: list[Edge] = []
    target_sites: list[UnresolvedSite] = []
    target_public_symbols: set[str] = set()
    target_status: AreaStatus

    if target_area in discovered_by_name:
        (
            target_nodes,
            target_edges,
            target_sites,
            target_public_symbols,
            target_status,
        ) = await _analyse_area(
            workspace=workspace,
            area_name=target_area,
            patterns=discovered_by_name[target_area],
        )
    else:
        # Unknown area — record a failure entry but still produce a
        # complete blob with the inherited data intact.
        target_status = AreaStatus(
            name=target_area,
            status="failed",
            error="unknown area",
            unresolved_dynamic_sites=0,
        )

    # ------------------------------------------------------------------
    # 3. Assemble the full node + edge set for cross-area re-validation.
    # ------------------------------------------------------------------
    all_nodes = inherited_nodes + target_nodes
    all_edges = inherited_edges + target_edges
    all_public_symbols = inherited_public_symbols | target_public_symbols

    # 4. Gap-fill the target area's unresolved sites (when a provider
    # is supplied).
    if provider is not None and target_sites:
        llm_edges = await _run_gap_fill_stage(
            workspace=workspace,
            provider=provider,
            nodes=all_nodes,
            per_area_sites=[(target_area, target_sites)],
            on_progress=on_progress,
        )
        all_edges.extend(llm_edges)

    # 5. Cross-language HTTP matching — re-runs across the whole graph
    # because a new route in the target area may now match an inherited
    # frontend call (and vice-versa). Inherited HTTP edges from the
    # previous blob are dropped first so we don't double-count.
    all_edges = [e for e in all_edges if e.kind != "http"]
    try:
        http_edges = await match_http_edges(
            workspace_path=workspace,
            nodes=all_nodes,
            provider=provider,
        )
        all_edges.extend(http_edges)
    except Exception as e:
        log.warning(
            "graph_partial_http_match_stage_failed",
            error=str(e),
            error_type=e.__class__.__name__,
        )

    # 6. Cross-area module-target resolution. Inherited edges already
    # carry resolved targets, so this is a no-op for them; it primarily
    # rewrites freshly-parsed target_edges that reference inherited
    # symbols via ``module:`` placeholders.
    all_edges = _resolve_cross_area_module_targets(all_edges, all_nodes)

    # 6b. Resolve bare ``module:<dotted>`` import endpoints to real
    # ``file:`` nodes (and drop edges that still don't resolve). See the
    # full-pipeline path for the rationale — the partial path needs the
    # same hygiene step so re-analysed areas don't reintroduce phantom
    # endpoints into the rendered graph.
    all_edges = _resolve_module_imports_to_files(all_edges, all_nodes)

    # 7. Boundary flagging — re-runs across the full edge set.
    rules = load_boundary_rules(workspace)
    all_edges = flag_violations(
        edges=all_edges,
        nodes=all_nodes,
        public_symbols=all_public_symbols,
        rules=rules,
    )

    # 8. Areas — preserved order: inherited statuses (in their previous
    # order) + the target area's fresh status. If the previous blob
    # already had a status entry for the target_area we drop it and
    # take the fresh one instead.
    statuses = [*inherited_statuses, target_status]
    _ = inherited_node_ids  # silence "assigned but unused" linters

    return RepoGraphBlob(
        commit_sha=commit_sha,
        generated_at=datetime.now(UTC),
        analyser_version=_ANALYSER_VERSION,
        areas=statuses,
        nodes=all_nodes,
        edges=all_edges,
        public_symbols=sorted(all_public_symbols),
        cycles=compute_cycles(all_edges),
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


def _resolve_module_imports_to_files(
    edges: list[Edge],
    nodes: list[Node],
) -> list[Edge]:
    """Rewrite bare ``module:<dotted>`` endpoints (no ``.<symbol>``
    suffix) on import edges to the corresponding ``file:`` node id when
    one exists, and drop edges whose endpoints still don't resolve to a
    real node.

    Per-file parsers emit import edges as ``module:<src> -> module:<tgt>``
    because they don't know about other files. The pipeline is the first
    place that can rewrite those placeholders against the full node set.
    The renderer (cytoscape) silently drops edges whose endpoints aren't
    in the node set AND its compound layout breaks when phantom edges
    are present, so unresolved external imports (e.g. ``module:os``) are
    dropped rather than rendered.

    Only bare ``module:<dotted>`` endpoints are touched. Non-``module:``
    ids (``file:``, ``area:``, ``<file>::<symbol>``) pass through
    unchanged. ``module:<dotted>.<symbol>`` shapes are also left alone —
    that space is owned by :func:`_resolve_cross_area_module_targets`,
    and the renderer falls back to a defensive orphan-edge filter for
    the residual unresolved-symbol case.
    """
    # Build a lookup from synthetic ``module:<dotted>`` ids to the real
    # ``file:`` node id by inverting the parser's file -> module mapping.
    file_module_to_id: dict[str, str] = {}
    for n in nodes:
        if n.kind != "file" or not n.file:
            continue
        module_dotted = _file_to_module(n.file)
        if module_dotted is None:
            continue
        file_module_to_id[f"module:{module_dotted}"] = n.id

    def _resolve(endpoint: str) -> str | None:
        """Return the rewritten endpoint, or ``None`` to signal the
        whole edge should be dropped."""
        if not endpoint.startswith("module:"):
            return endpoint
        rewritten = file_module_to_id.get(endpoint)
        if rewritten is not None:
            return rewritten
        # ``module:<dotted>.<symbol>`` (symbol-level placeholder) — leave
        # alone whether or not the file is known locally. Same-area
        # contracts (e.g. ``module:agent_area.base.Animal`` on inherits
        # edges) and the boundary flagger both depend on this shape.
        # Residual phantoms here are caught by the renderer's defensive
        # orphan-edge filter.
        dotted = endpoint[len("module:") :]
        if "." in dotted:
            return endpoint
        # Bare ``module:<dotted>`` with no file match — an external
        # module reference (``module:os``, ``module:react``) that can't
        # be rendered. Drop the edge.
        return None

    rewritten: list[Edge] = []
    for edge in edges:
        new_source = _resolve(edge.source)
        new_target = _resolve(edge.target)
        if new_source is None or new_target is None:
            continue
        if new_source == edge.source and new_target == edge.target:
            rewritten.append(edge)
            continue
        rewritten.append(
            Edge(
                source=new_source,
                target=new_target,
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
    on_progress: GapFillProgressHook | None = None,
) -> list[Edge]:
    """One-shot gap-fill across every unresolved site, in parallel.

    Each site gets exactly one :func:`gap_fill_site` call — there is no
    multi-turn fallback. Sites are dispatched concurrently bounded by
    :data:`_GAP_FILL_CONCURRENCY`; the candidate pool is precomputed
    once per area so the gather doesn't re-rank for every site.

    Validation order (unchanged from prior implementation):
      1. Soft filter inside gap-fill: target must be in candidate pool.
      2. ``validate_citation`` against the workspace.
      3. ``validate_target`` against the final node set.

    Step 1 is a fast belt-and-braces check; steps 2 + 3 are the
    unconditional gates promised by ADR-016 §3.

    Per-site exceptions are caught and surface as zero edges for that
    site — they never fail the whole stage.

    ``on_progress`` (when supplied) fires after each site completes
    with the cumulative ``(done, total)``. The hook is awaited
    serialised under an internal lock so callers don't have to make
    their tracker concurrent-safe; if the hook raises, the run still
    completes — progress is best-effort UX, not load-bearing state.
    """
    total = sum(len(sites) for _, sites in per_area_sites)
    if total == 0:
        return []

    candidates_by_area: dict[str, list[Node]] = {
        area: _candidate_pool_for_area(nodes, area) for area, _ in per_area_sites
    }

    semaphore = asyncio.Semaphore(_GAP_FILL_CONCURRENCY)
    progress_lock = asyncio.Lock()
    done = 0

    async def _gap_fill_one(
        area_name: str,
        site: UnresolvedSite,
    ) -> list[Edge]:
        nonlocal done
        async with semaphore:
            try:
                llm_edges = await gap_fill_site(
                    provider=provider,
                    workspace_path=workspace,
                    site=site,
                    candidate_nodes=candidates_by_area[area_name],
                )
            except Exception as e:
                log.warning(
                    "graph_gap_fill_site_unexpected",
                    site=site.containing_node_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                )
                llm_edges = []
        survivors = _validate_edges(workspace, llm_edges, nodes)
        if on_progress is not None:
            async with progress_lock:
                done += 1
                try:
                    await on_progress(done, total)
                except Exception as e:
                    log.warning(
                        "graph_gap_fill_progress_hook_failed",
                        error=str(e),
                        error_type=e.__class__.__name__,
                    )
        return survivors

    tasks = [
        _gap_fill_one(area_name, site) for area_name, sites in per_area_sites for site in sites
    ]
    results = await asyncio.gather(*tasks)
    return [edge for batch in results for edge in batch]


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
