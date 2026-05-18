"""Parser registry — one parser per language, dispatched by file extension.

Phase 2 of ADR-016 ships only the Python parser. Phase 4 adds TypeScript;
that's a single new file (``parsers/typescript.py``) plus one extension
mapping in :data:`_REGISTRY`. The pipeline never grows a language switch —
all dispatch funnels through :func:`parser_for`.

The :class:`Parser` ABC pins the shape of a parser. Implementations are
synchronous and pure with respect to the input bytes; they take no I/O of
their own (the pipeline reads file bytes once and hands them in). This
keeps the parsers trivially testable and the pipeline the single owner of
filesystem access.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.graph_analyzer.types import UnresolvedSite
    from shared.types import Edge, Node


@dataclass
class ParseResult:
    """One file's contribution to the graph.

    ``unresolved_sites`` lists every dispatch site the parser detected
    but could not resolve statically (registry dicts, ``getattr``,
    decorator-driven dispatch, attribute calls on imported modules,
    etc.). Phase 2 emitted only a count; Phase 3 emits the sites
    themselves so the gap-fill stage can feed each one to the LLM.

    ``public_symbols`` (Phase 5 — ADR-016 §7) is the set of node ids the
    parser considers part of this file's public surface for cross-area
    consumption. Determined by language conventions (Python: ``__all__``
    plus the no-underscore-prefix rule; TypeScript: ``export`` plus
    path-segment / filename rules). The pipeline unions these across all
    files in an area to compute the area's public surface, then flags
    cross-area edges that reach non-public symbols.

    Phase 2 callers that only need the count can read
    ``len(parse_result.unresolved_sites)``.
    """

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    unresolved_sites: list[UnresolvedSite] = field(default_factory=list)
    public_symbols: set[str] = field(default_factory=set)

    @property
    def unresolved_dynamic_sites(self) -> int:
        """Backwards-compatible accessor — count of unresolved sites.

        Kept as a property so existing call sites continue to read
        ``result.unresolved_dynamic_sites`` without change while the
        underlying data has been promoted from a plain int to a real
        list. The :class:`shared.types.AreaStatus` public blob still
        carries this as an ``int`` field — only the parser/pipeline
        seam exposes the richer shape.
        """
        return len(self.unresolved_sites)


class Parser(ABC):
    """Single-file parser contract.

    Implementations are stateless and reusable across files; the pipeline
    constructs one per language and reuses it for every file in the area.
    """

    #: Tuple of file extensions (with leading dot) this parser handles.
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def parse_file(
        self,
        *,
        rel_path: str,
        area: str,
        source: bytes,
    ) -> ParseResult:
        """Parse one file's source bytes.

        Args:
            rel_path: Path relative to the workspace root (used as the
                ``file`` field on every edge / node).
            area: Area name this file belongs to — copied onto every node
                so query callers can filter without walking parents.
            source: Raw file bytes (parsers do their own decode).
        """


# ----------------------------------------------------------------------
# Extension → parser-class registry. The pipeline imports parser_for(ext)
# to obtain a singleton per file. Adding TypeScript in Phase 4: import
# the new class and add the ``".ts": TypeScriptParser`` entry below.
# ----------------------------------------------------------------------

from agent.graph_analyzer.parsers.python import PythonParser  # noqa: E402
from agent.graph_analyzer.parsers.typescript import TypeScriptParser  # noqa: E402

_REGISTRY: dict[str, type[Parser]] = {
    ".py": PythonParser,
    ".ts": TypeScriptParser,
    ".tsx": TypeScriptParser,
}

# Singletons — parsing is stateless so we share an instance per process.
_INSTANCES: dict[type[Parser], Parser] = {}


def parser_for(file_path: str) -> Parser | None:
    """Return the parser for ``file_path``'s extension, or ``None`` if no
    parser is registered for that language."""
    ext = os.path.splitext(file_path)[1].lower()
    cls = _REGISTRY.get(ext)
    if cls is None:
        return None
    inst = _INSTANCES.get(cls)
    if inst is None:
        inst = cls()
        _INSTANCES[cls] = inst
    return inst


def supported_extensions() -> tuple[str, ...]:
    """All extensions for which a parser is registered."""
    return tuple(sorted(_REGISTRY))


__all__ = [
    "ParseResult",
    "Parser",
    "parser_for",
    "supported_extensions",
]
