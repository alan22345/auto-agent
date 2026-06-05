"""Import-cycle detection via iterative Tarjan SCC (ADR-016 Phase 9).

Type-only import filtering is not yet possible (parsers don't tag type-only
imports); deferred to a future task.

Only ``imports`` edges are considered. ``calls`` edges (representing function
call graphs / recursion) are intentionally excluded — recursive calls are
legitimate and not a structural dependency problem.

The iterative Tarjan implementation avoids Python's default recursion limit,
which would be unsafe for large module graphs (thousands of nodes).

Public API
----------
compute_cycles(edges) -> list[DependencyCycle]
    Given the full edge list from the pipeline, return all import cycles
    detected via Tarjan's strongly-connected-components algorithm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shared.types import DependencyCycle, EdgeEvidence

if TYPE_CHECKING:
    from shared.types import Edge


def compute_cycles(edges: list[Edge]) -> list[DependencyCycle]:
    """Detect all import cycles in ``edges`` using iterative Tarjan SCC.

    Only edges with ``kind == "imports"`` are considered. All other edge
    kinds are silently ignored.

    A cycle is:

    * Any SCC (strongly connected component) with more than one member, OR
    * A 1-member SCC that has a self-loop (an edge whose ``source == target``).

    Returns
    -------
    list[DependencyCycle]
        Sorted deterministically by cycle id. No duplicate cycle objects.

    Each :class:`~shared.types.DependencyCycle` has:

    * ``kind="import"``
    * ``members`` — sorted vertex ids participating in the cycle.
    * ``closing_edges`` — one :class:`~shared.types.EdgeEvidence` per
      ``imports`` edge whose source **and** target are both in this SCC
      (i.e. one entry per edge, including parallel edges between the same
      pair of modules), sorted by ``(file, line)``.
    * ``id`` — stable deterministic id: ``"cycle:" + "|".join(sorted_members)``.
    """
    # Collect only imports edges.
    import_edges = [e for e in edges if e.kind == "imports"]

    if not import_edges:
        return []

    # Build adjacency list and collect all unique vertices.
    adj: dict[str, list[str]] = {}
    vertices: set[str] = set()
    for e in import_edges:
        vertices.add(e.source)
        vertices.add(e.target)
        adj.setdefault(e.source, []).append(e.target)

    # Run iterative Tarjan SCC.
    sccs = _tarjan_iterative(vertices, adj)

    # Build a set of self-loop sources for the 1-member SCC check.
    self_loops: set[str] = {e.source for e in import_edges if e.source == e.target}

    cycles: list[DependencyCycle] = []
    for scc in sccs:
        if len(scc) == 1:
            # Only a cycle if the single member has a self-loop.
            node = next(iter(scc))
            if node not in self_loops:
                continue

        sorted_members = sorted(scc)
        scc_set = scc  # already a set

        # Collect closing edges: one evidence entry per in-cycle import edge.
        # Each edge contributes exactly its own evidence — no lookup by (source, target)
        # to avoid N² expansion when parallel edges share the same pair.
        closing: list[EdgeEvidence] = []
        for e in import_edges:
            if e.source in scc_set and e.target in scc_set:
                closing.append(e.evidence)

        closing_sorted = sorted(closing, key=lambda ev: (ev.file, ev.line))

        cycle_id = "cycle:" + "|".join(sorted_members)
        cycles.append(
            DependencyCycle(
                id=cycle_id,
                kind="import",
                members=sorted_members,
                closing_edges=closing_sorted,
            )
        )

    # Sort cycles deterministically by id.
    cycles.sort(key=lambda c: c.id)
    return cycles


# ---------------------------------------------------------------------------
# Iterative Tarjan SCC
# ---------------------------------------------------------------------------


def _tarjan_iterative(
    vertices: set[str],
    adj: dict[str, list[str]],
) -> list[set[str]]:
    """Compute all strongly-connected components using an iterative variant
    of Tarjan's algorithm.

    The iterative form avoids Python's call-stack limit, which would be
    unsafe for large module graphs (thousands of nodes).

    Returns a list of sets, each set being one SCC.
    """
    index_counter = [0]
    stack: list[str] = []
    lowlink: dict[str, int] = {}
    index: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    sccs: list[set[str]] = []

    # Iterative Tarjan uses an explicit call stack that mirrors the recursive
    # DFS. Each frame stores (node, iterator_over_neighbours, lowlink_state).
    # We use a list-of-tuples as the work stack.

    def _strongconnect(start: str) -> None:
        # Initialise start node.
        index[start] = lowlink[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack[start] = True

        # work_stack: list of (node, neighbour_offset)
        work: list[tuple[str, int]] = [(start, 0)]

        while work:
            v, ni = work[-1]
            neighbours = adj.get(v, [])

            if ni < len(neighbours):
                w = neighbours[ni]
                work[-1] = (v, ni + 1)  # advance the neighbour pointer

                if w not in index:
                    # Tree edge: discover w.
                    index[w] = lowlink[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, 0))
                elif on_stack.get(w, False):
                    # Back edge: w is on the current DFS stack — update lowlink.
                    if lowlink[w] < lowlink[v]:
                        lowlink[v] = lowlink[w]
            else:
                # All neighbours of v exhausted — pop v.
                work.pop()

                if work:
                    # Propagate lowlink to parent.
                    parent = work[-1][0]
                    if lowlink[v] < lowlink[parent]:
                        lowlink[parent] = lowlink[v]

                # Root of an SCC?
                if lowlink[v] == index[v]:
                    scc: set[str] = set()
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        scc.add(w)
                        if w == v:
                            break
                    sccs.append(scc)

    for v in sorted(vertices):  # sorted for determinism
        if v not in index:
            _strongconnect(v)

    return sccs


__all__ = ["compute_cycles"]
