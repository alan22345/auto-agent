"""Citation + target validation for LLM-emitted edges (ADR-016 Phase 3 §3).

Every edge that the gap-fill or agent-escape stages produce must clear
**both** checks below before it lands in the graph blob:

* :func:`validate_citation` — open the cited file, look at
  ``evidence.line ± 2`` lines, and confirm that
  ``evidence.snippet.strip()`` is a substring of any line in that window.
  Fuzziness exists because LLMs are bad at exact line numbers; the
  substring check stays strict enough to catch obvious hallucinations.
* :func:`validate_target` — confirm ``edge.target`` is a real node id
  in the graph. Targets the LLM made up never get an edge.

These are unconditional. There is no "skip validation in dev" flag.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from shared.types import Edge, Node

log = structlog.get_logger(__name__)

#: Number of lines of slack on either side of the cited line. Strikes a
#: balance between LLM-line-counting forgiveness and not letting a
#: snippet match an unrelated similarly-named line elsewhere in the file.
_CITATION_FUZZ_LINES = 2


def validate_citation(workspace_path: str, edge: Edge) -> bool:
    """Return True iff the edge's cited snippet appears within ±2 lines
    of its declared line in the cited file.

    Failure modes (each returns ``False``, never raises):
      * cited file does not exist or is unreadable;
      * cited line is ``<= 0``;
      * snippet (after strip) is empty;
      * snippet is not a substring of any line in the window.

    Args:
        workspace_path: Absolute path to the workspace root. The
            edge's ``evidence.file`` is workspace-relative.
        edge: The candidate edge.
    """
    snippet = (edge.evidence.snippet or "").strip()
    if not snippet:
        log.info(
            "graph_citation_dropped",
            reason="empty_snippet",
            file=edge.evidence.file,
            target=edge.target,
        )
        return False
    line = edge.evidence.line
    if line <= 0:
        log.info(
            "graph_citation_dropped",
            reason="non_positive_line",
            file=edge.evidence.file,
            line=line,
            target=edge.target,
        )
        return False

    abs_path = os.path.join(workspace_path, edge.evidence.file)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        log.info(
            "graph_citation_dropped",
            reason="file_unreadable",
            file=edge.evidence.file,
            error=str(e),
            target=edge.target,
        )
        return False

    start = max(0, line - 1 - _CITATION_FUZZ_LINES)
    end = min(len(lines), line - 1 + _CITATION_FUZZ_LINES + 1)
    window = lines[start:end]
    for source_line in window:
        if snippet in source_line:
            return True

    log.info(
        "graph_citation_dropped",
        reason="snippet_not_in_window",
        file=edge.evidence.file,
        line=line,
        target=edge.target,
        snippet=snippet[:80],
    )
    return False


def validate_target(edge: Edge, nodes: list[Node]) -> bool:
    """Return True iff ``edge.target`` matches a node id in ``nodes``.

    Linear scan over ``nodes`` keeps the API simple. Callers running
    validation for many edges should cache the id set in their own
    scope rather than re-hashing here.
    """
    if not nodes:
        log.info(
            "graph_target_dropped",
            reason="no_nodes",
            target=edge.target,
        )
        return False
    target = edge.target
    for node in nodes:
        if node.id == target:
            return True
    log.info(
        "graph_target_dropped",
        reason="target_not_in_graph",
        target=target,
    )
    return False


__all__ = ["validate_citation", "validate_target"]
