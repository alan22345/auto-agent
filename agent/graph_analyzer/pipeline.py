"""End-to-end pipeline for ADR-016 Phase 2.

Stages (matches the ADR §10 scope for Phase 2):

1. Resolve area layout — read ``.auto-agent/graph.yml`` if present, else
   default to top-level directories (with a stable skip-list).
2. For each area: walk files, dispatch by extension to the right parser
   (via :func:`agent.graph_analyzer.parsers.parser_for`), accumulate
   nodes / edges / dynamic-site count.
3. Per-area failure isolation — a parser exception marks the area
   ``failed`` and continues with the next area.
4. Assemble the :class:`shared.types.RepoGraphBlob`. Overall status:
   ``ok`` if every area succeeded, ``partial`` if some did, ``failed``
   if none did.

Phase 2 does *not* call into the LLM. Phase 3 lands the gap-fill stage
between (2) and (4).
"""

from __future__ import annotations

import fnmatch
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
import yaml

from agent.graph_analyzer.parsers import parser_for, supported_extensions
from shared.types import AreaStatus, Edge, Node, RepoGraphBlob

if TYPE_CHECKING:
    from agent.graph_analyzer.parsers import ParseResult

log = structlog.get_logger(__name__)

# Phase 2 analyser version. Bumped on any change to the wire output —
# new edge kind, schema additions, parser behaviour change. The string
# lands in ``RepoGraph.analyser_version`` and the blob alike.
_ANALYSER_VERSION = "phase2-python-0.2.0"

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
) -> tuple[list[Node], list[Edge], int, AreaStatus]:
    """Run the parser dispatch over one area.

    Returns the area's nodes, edges, dynamic-site count, and an
    :class:`AreaStatus` (``ok`` on success; ``failed`` if a parser
    exception bubbled up). Individual files that the parser handled
    gracefully (e.g. tree-sitter ERROR-node recovery) do NOT fail the
    area — only an unhandled exception does.
    """
    nodes: list[Node] = []
    edges: list[Edge] = []
    dynamic_sites = 0

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
            dynamic_sites += pr.unresolved_dynamic_sites
    except Exception as e:
        log.exception("graph_area_failed", area=area_name, error=str(e))
        return (
            nodes,
            edges,
            dynamic_sites,
            AreaStatus(
                name=area_name,
                status="failed",
                error=str(e) or e.__class__.__name__,
                unresolved_dynamic_sites=dynamic_sites,
            ),
        )

    return (
        nodes,
        edges,
        dynamic_sites,
        AreaStatus(
            name=area_name,
            status="ok",
            error=None,
            unresolved_dynamic_sites=dynamic_sites,
        ),
    )


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def run_pipeline(*, workspace: str, commit_sha: str) -> RepoGraphBlob:
    """Run the full Phase 2 pipeline against ``workspace``.

    The caller is responsible for cloning / fetching / resetting the
    workspace and supplying the resolved ``commit_sha``. The pipeline
    only reads — never writes, fetches, or shells out.
    """
    areas = _discover_areas(workspace)

    all_nodes: list[Node] = []
    all_edges: list[Edge] = []
    statuses: list[AreaStatus] = []

    for name, patterns in areas:
        nodes, edges, _dyn, status = _analyse_area(
            workspace=workspace,
            area_name=name,
            patterns=patterns,
        )
        all_nodes.extend(nodes)
        all_edges.extend(edges)
        statuses.append(status)

    return RepoGraphBlob(
        commit_sha=commit_sha,
        generated_at=datetime.now(UTC),
        analyser_version=_ANALYSER_VERSION,
        areas=statuses,
        nodes=all_nodes,
        edges=all_edges,
    )


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
