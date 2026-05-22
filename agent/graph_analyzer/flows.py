"""Capability / flow derivation (Phase 1).

Top-level entry point is :func:`derive_flow_blob`, which composes:

  detect_entry_points  →  trace_flow per entry  →  classify_terminal
       →  hash file sets  →  assemble FlowJsonBlob

Phase 1 leaves capability and flow names as ``None`` — Phase 2 labels
them via an LLM call. Phase 1 emits exactly one capability with
``id="unlabeled"`` containing every derived flow.

The trace is pure (no I/O, no DB): given a finished RepoGraphBlob, it
produces a deterministic FlowJsonBlob. The recompute endpoint reads
the blob from the DB, runs derivation, writes the result back. The
file-hash step in :func:`derive_flow_blob` is the only stage that
touches disk.
"""
from __future__ import annotations

from collections import defaultdict

from shared.types import (
    EntryPoint,
    FlowStep,
    Node,
    RepoGraphBlob,
)

# Spec §10: hard cap on per-flow step count. Anything past this is
# dropped; the UI may render a "+N hidden" marker in Phase 3.
MAX_FLOW_STEPS = 50

# Spec §3 step 3: branches inlined to depth-3 past the branch root.
BRANCH_INLINE_DEPTH = 3


def _outgoing_call_targets(edges_by_source: dict[str, list[str]], node_id: str) -> list[str]:
    return edges_by_source.get(node_id, [])


def trace_flow(blob: RepoGraphBlob, entry_point: EntryPoint) -> list[FlowStep]:
    """Forward-trace call edges from *entry_point* into an ordered step list.

    BFS over ``kind="calls"`` edges, deterministic in the order edges
    appear in the blob. Branches mark their root and are walked up to
    ``BRANCH_INLINE_DEPTH`` past the root depth. Cycles record a
    cycle-back step and stop. The walk hard-caps at ``MAX_FLOW_STEPS``.
    """
    edges_by_source: dict[str, list[str]] = defaultdict(list)
    for edge in blob.edges:
        if edge.kind == "calls":
            edges_by_source[edge.source].append(edge.target)

    nodes_by_id: dict[str, Node] = {n.id: n for n in blob.nodes}

    steps: list[FlowStep] = []
    # Frontier entries: (node_id, depth, branch_root_depth_or_None).
    frontier: list[tuple[str, int, int | None]] = [(entry_point.node_id, 0, None)]
    on_path: set[str] = set()

    while frontier and len(steps) < MAX_FLOW_STEPS:
        node_id, depth, branch_root_depth = frontier.pop(0)
        if node_id in on_path:
            # Cycle: emit a cycle-back terminal step but do not expand.
            steps.append(FlowStep(node_id=node_id, depth=depth, is_cycle_back=True))
            continue

        targets = _outgoing_call_targets(edges_by_source, node_id)
        is_branch_root = len(targets) >= 2
        steps.append(
            FlowStep(node_id=node_id, depth=depth, is_branch_root=is_branch_root),
        )
        on_path.add(node_id)

        if not targets:
            continue

        # If the current node is itself a branch root, root the depth cap
        # at depth+1 (the first child on the branch — what the spec and
        # tests call "the branch root").  Otherwise inherit the existing
        # branch-root depth (None on the dominant / trunk path).
        new_root_depth = depth + 1 if is_branch_root else branch_root_depth

        for target in targets:
            if target not in nodes_by_id:
                continue
            child_depth = depth + 1
            if new_root_depth is not None and child_depth - new_root_depth > BRANCH_INLINE_DEPTH:
                continue
            frontier.append((target, child_depth, new_root_depth))

    return steps


__all__ = ["BRANCH_INLINE_DEPTH", "MAX_FLOW_STEPS", "trace_flow"]
