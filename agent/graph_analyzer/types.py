"""Pipeline-internal types for ADR-016 Phase 3 gap-fill.

These types are **not** part of the public `RepoGraphBlob` schema — they
flow from the parser to the gap-fill stage inside one pipeline run and
are dropped before the blob is assembled. Keeping them out of
`shared/types.py` honours the rule that the wire schema is locked and
Phase 3 does not change it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Heuristic label the parser attaches to an unresolved call site so the
#: LLM gap-fill prompt can be tailored / monitored. Values:
#:
#: * ``"registry"`` — dict / list / set indexed call like
#:   ``HANDLERS[name](payload)``.
#: * ``"getattr"`` — ``getattr(obj, name)(...)``-style dispatch.
#: * ``"decorator_routed"`` — call where the parser sees a known
#:   route-decorator pattern (FastAPI ``@app.route(...)`` etc.).
#: * ``"dict_call"`` — bare ``foo.bar()`` where the receiver is a
#:   module-bound name; covers the very common "attribute call on an
#:   imported module" case.
#: * ``"unknown"`` — everything else the parser can't classify but
#:   couldn't resolve statically.
PatternHint = Literal[
    "registry",
    "getattr",
    "decorator_routed",
    "dict_call",
    "unknown",
]


@dataclass
class UnresolvedSite:
    """One detected dispatch site the parser couldn't resolve statically.

    Phase 3's LLM gap-fill receives one of these per call site and
    decides whether to emit one or more :class:`shared.types.Edge`
    instances with ``source_kind="llm"``.

    Fields are workspace-relative and ready to be re-opened on disk for
    citation validation.
    """

    file: str
    """Workspace-relative file path of the dispatch site."""

    line: int
    """1-indexed line number of the call expression."""

    snippet: str
    """The source line containing the call (already stripped)."""

    containing_node_id: str
    """Graph node id of the function/method that owns this call. Used as
    the ``source`` of every edge the LLM emits for this site."""

    surrounding_code: str
    """Roughly 30 lines of source centred on the dispatch site. The
    parser captures this once so the gap-fill and escape stages don't
    have to re-read the file just to assemble a prompt."""

    pattern_hint: PatternHint
    """One-word classification of the dispatch pattern. Used purely to
    shape the LLM prompt — the validator does not consume it."""


__all__ = ["PatternHint", "UnresolvedSite"]
