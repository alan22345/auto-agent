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
    from shared.types import Edge, Node


@dataclass
class ParseResult:
    """One file's contribution to the graph.

    ``unresolved_dynamic_sites`` is the count of detected dispatch sites
    the parser could *not* resolve statically (registry dicts, ``getattr``,
    decorator-driven dispatch, attribute calls on imported modules,
    etc.). Phase 2 counts but does not resolve them — Phase 3's LLM
    gap-fill turns these into edges.
    """

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    unresolved_dynamic_sites: int = 0


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

_REGISTRY: dict[str, type[Parser]] = {
    ".py": PythonParser,
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
