"""Dead-code detection for the code-graph pipeline (ADR-016 Phase 10 §4).

Exposes one pure function:

    compute_dead_code(blob: RepoGraphBlob) -> list[DeadCodeFinding]

Two finding kinds are produced:

``unused_export``
    An exported symbol (present in ``RepoGraphBlob.public_symbols``) of kind
    ``function`` or ``class`` that has no incoming ``calls`` or ``inherits``
    edge from a node in a *different* file.  Same-file callers do not count.

``unused_file``
    A ``file:`` node with no surviving incoming ``imports`` edges after
    resolution (all import targets are ``file:`` ids post-pipeline) and no
    entry-point node living inside the file.  Common entry/test filenames are
    excluded to avoid noisy false positives.

The function is purely deterministic: no I/O, no LLM calls. Output is sorted
by ``(kind, target)`` and contains no duplicates.

Known limitations
-----------------
a) **Dynamic imports** — ``importlib.import_module(name)`` and the dynamic
   ``require(expr)`` pattern produce no AST edge, so a file imported only
   dynamically will be mis-flagged as ``unused_file``.  The exclusion list
   (``main.py``, ``__main__.py``, ``app.py``, etc.) and the entry-point filter
   reduce the practical impact of this gap.

b) **Dependency-level findings deferred** — the pipeline drops external/
   third-party import edges during resolution (``_resolve_module_imports_to_files``
   filters out ``module:<bare>`` endpoints that have no local ``file:`` match).
   There is therefore no edge data to drive ``unused_dependency`` or
   ``undeclared_dependency`` detection.  Those kinds are reserved in the schema
   for a future pass that captures the pre-drop edge set.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agent.graph_analyzer.entry_points import detect_entry_points
from shared.types import DeadCodeFinding

if TYPE_CHECKING:
    from shared.types import RepoGraphBlob

# ---------------------------------------------------------------------------
# Filename exclusions for unused_file detection
# ---------------------------------------------------------------------------

# Basenames that are never flagged regardless of import count.
_EXCLUDED_BASENAMES: frozenset[str] = frozenset(
    {
        "__init__.py",
        "__main__.py",
        "conftest.py",
        "setup.py",
        "main.py",
        "run.py",
        "app.py",
        "manage.py",
    }
)

# Test-file patterns: ``test_*.py``, ``*_test.py``, ``*.test.ts``, ``*.spec.ts``.
_TEST_FILE_RE = re.compile(
    r"(?:^|/)(?:test_.+\.py|.+_test\.py|.+\.test\.(?:ts|tsx|js|jsx)|.+\.spec\.(?:ts|tsx|js|jsx))$"
)


def _is_excluded_file(path: str) -> bool:
    """Return ``True`` if *path* matches an exclusion rule for unused_file."""
    basename = path.rsplit("/", 1)[-1]
    if basename in _EXCLUDED_BASENAMES:
        return True
    return bool(_TEST_FILE_RE.search(path))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_module_symbol_lookup(nodes: list) -> dict[str, str]:
    """Build a map from ``module:<dotted>.<symbol>`` → node id for all
    class/function nodes.

    The pipeline's :func:`_resolve_cross_area_module_targets` rewrites
    cross-area ``module:`` targets to canonical ``<file>::<symbol>`` ids,
    but leaves **same-area** ``module:`` targets untouched.  When computing
    external callers we must therefore also resolve ``module:`` targets
    ourselves so that same-area calls edges (e.g.
    ``consumer.py::do_work → module:used_area.utils.used_helper``)
    are correctly mapped to their target node id.
    """
    from agent.graph_analyzer.pipeline import _file_to_module  # local import avoids circular

    lookup: dict[str, str] = {}
    for n in nodes:
        if n.kind not in ("class", "function"):
            continue
        if not n.file:
            continue
        suffix = n.id.split("::", 1)[-1]
        if "." in suffix:
            continue
        module_dotted = _file_to_module(n.file)
        if module_dotted is None:
            continue
        lookup[f"module:{module_dotted}.{suffix}"] = n.id
    return lookup


def compute_dead_code(blob: RepoGraphBlob) -> list[DeadCodeFinding]:
    """Compute dead-code findings for *blob*.

    Calls :func:`~agent.graph_analyzer.entry_points.detect_entry_points`
    internally to exclude entry-point nodes from ``unused_export`` findings
    and to exclude files that own an entry-point from ``unused_file``
    findings.

    **Precondition:** the blob's ``imports`` edges are expected to have been
    resolved to ``file:`` ids by the pipeline
    (:func:`~agent.graph_analyzer.pipeline._resolve_module_imports_to_files`)
    before calling this function.  The function defends against unresolved
    ``module:<dotted>`` import targets (mapping them to their file node when a
    matching ``file:`` node exists), but callers should run the full pipeline
    for maximally accurate results — unresolved edges for third-party modules
    are silently ignored, which is correct, but any first-party module that
    slips through unresolved will benefit from this defence rather than being
    falsely flagged.

    Returns a deduplicated, deterministically sorted
    ``list[DeadCodeFinding]``.
    """
    # ------------------------------------------------------------------
    # Preconditions: build lookup structures once.
    # ------------------------------------------------------------------

    # Entry-point node ids — used to exclude from both finding kinds.
    entry_points = detect_entry_points(blob)
    entry_point_ids: set[str] = {ep.node_id for ep in entry_points}

    # Node id → Node
    node_by_id = {n.id: n for n in blob.nodes}

    # ``module:<dotted>.<symbol>`` → canonical node id.
    # Needed to resolve calls edges that still carry unresolved module targets
    # (same-area calls are NOT rewritten by _resolve_cross_area_module_targets).
    module_symbol_lookup = _build_module_symbol_lookup(blob.nodes)

    # File path → set of entry-point node ids that live in that file.
    # Used for unused_file: if any entry-point node lives in a file, skip it.
    entry_point_files: set[str] = set()
    for ep_id in entry_point_ids:
        node = node_by_id.get(ep_id)
        if node and node.file:
            entry_point_files.add(node.file)

    # ------------------------------------------------------------------
    # 1. unused_export detection
    # ------------------------------------------------------------------
    # Build the set of (source file → set of targets) for incoming
    # calls/inherits edges.  We want to know, for each symbol s, whether
    # there is at least one incoming edge whose *source node* lives in a
    # different file.

    # Map target node id → set of source file paths that call/inherit it
    # from a *different* file.
    external_callers: dict[str, set[str]] = {}
    for edge in blob.edges:
        if edge.kind not in {"calls", "inherits"}:
            continue
        # Resolve the edge target — it may still be a ``module:`` placeholder
        # for same-area calls (the pipeline only rewrites cross-area ones).
        raw_target = edge.target
        resolved_target = module_symbol_lookup.get(raw_target, raw_target)

        target_node = node_by_id.get(resolved_target)
        source_node = node_by_id.get(edge.source)
        if target_node is None or source_node is None:
            continue
        if target_node.file is None:
            continue
        # "external" = source lives in a different file than the target.
        if source_node.file != target_node.file:
            external_callers.setdefault(resolved_target, set()).add(source_node.file or "")

    findings: list[DeadCodeFinding] = []

    for symbol_id in blob.public_symbols:
        node = node_by_id.get(symbol_id)
        if node is None:
            continue
        # Only function and class nodes are considered.
        if node.kind not in {"function", "class"}:
            continue
        # Skip entry-point nodes.
        if symbol_id in entry_point_ids:
            continue
        # Skip decorated nodes — decorators imply runtime wiring.
        if node.decorators:
            continue
        # Flag if there are no external callers or subclassers.
        if not external_callers.get(symbol_id):
            findings.append(
                DeadCodeFinding(
                    kind="unused_export",
                    target=symbol_id,
                    file=node.file,
                    reason="exported but no external caller or subclass",
                )
            )

    # ------------------------------------------------------------------
    # 2. unused_file detection
    # ------------------------------------------------------------------
    # After _resolve_module_imports_to_files in the pipeline, all surviving
    # first-party imports edges target ``file:<path>`` node ids directly.
    # Build the set of file node ids that are targeted by at least one
    # imports edge.
    #
    # Defence against unresolved ``module:<dotted>`` targets: build a reverse
    # map from ``module:<dotted-of-file>`` → ``file:<path>`` id for every
    # file node, then when encountering a ``module:`` import target attempt
    # to resolve it to its canonical file id before recording it.  This
    # prevents false-positive unused_file findings when the pipeline is called
    # without prior import resolution (e.g. from tests or the task proposer).
    from agent.graph_analyzer.pipeline import _file_to_module  # local import avoids circular

    module_to_file_id: dict[str, str] = {}
    for n in blob.nodes:
        if n.kind != "file" or not n.file:
            continue
        dotted = _file_to_module(n.file)
        if dotted is not None:
            module_to_file_id[f"module:{dotted}"] = n.id

    imported_file_ids: set[str] = set()
    for edge in blob.edges:
        if edge.kind != "imports":
            continue
        target = edge.target
        if target.startswith("module:"):
            # Attempt to resolve to a file id; fall back to the raw target so
            # that the unresolvable (third-party) case is still a no-op.
            target = module_to_file_id.get(target, target)
        imported_file_ids.add(target)

    for node in blob.nodes:
        if node.kind != "file":
            continue
        if node.file is None:
            continue
        # Apply exclusion list.
        if _is_excluded_file(node.file):
            continue
        # Skip if a file contains an entry-point node.
        if node.file in entry_point_files:
            continue
        # Flag if no imports edge targets this file node.
        if node.id not in imported_file_ids:
            findings.append(
                DeadCodeFinding(
                    kind="unused_file",
                    target=node.id,
                    file=node.file,
                    reason="no module imports this file and it defines no entry point",
                )
            )

    # ------------------------------------------------------------------
    # Sort deterministically and return.
    # ------------------------------------------------------------------
    findings.sort(key=lambda f: (f.kind, f.target))
    return findings


__all__ = ["compute_dead_code"]
