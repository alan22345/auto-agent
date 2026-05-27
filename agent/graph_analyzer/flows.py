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

import hashlib
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from agent.graph_analyzer.entry_points import detect_entry_points

if TYPE_CHECKING:
    from pathlib import Path
from shared.types import (
    Capability,
    EntryPoint,
    EntryPointKind,
    Flow,
    FlowJsonBlob,
    FlowStep,
    Node,
    RepoGraphBlob,
    TerminalKind,
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
    # Frontier entries: (node_id, depth, branch_root_depth_or_None, ancestors).
    # ancestors: frozenset of node ids on the path from entry to the current
    # node — used to detect true back-edges (cycles) vs. diamond convergence.
    frontier: list[tuple[str, int, int | None, frozenset[str]]] = [
        (entry_point.node_id, 0, None, frozenset()),
    ]
    visited: set[str] = set()

    while frontier and len(steps) < MAX_FLOW_STEPS:
        node_id, depth, branch_root_depth, ancestors = frontier.pop(0)

        if node_id in ancestors:
            # True cycle: this node is an ancestor of itself on this path.
            steps.append(FlowStep(node_id=node_id, depth=depth, is_cycle_back=True))
            continue

        if node_id in visited:
            # Diamond convergence — already emitted as a normal step; skip.
            continue
        visited.add(node_id)

        targets = _outgoing_call_targets(edges_by_source, node_id)
        is_branch_root = len(targets) >= 2
        steps.append(
            FlowStep(node_id=node_id, depth=depth, is_branch_root=is_branch_root),
        )

        if not targets:
            continue

        # If the current node is itself a branch root, root the depth cap
        # at depth+1 (the first child on the branch — what the spec and
        # tests call "the branch root").  Otherwise inherit the existing
        # branch-root depth (None on the dominant / trunk path).
        new_root_depth = depth + 1 if is_branch_root else branch_root_depth
        new_ancestors = ancestors | {node_id}

        for target in targets:
            if target not in nodes_by_id:
                continue
            child_depth = depth + 1
            if new_root_depth is not None and child_depth - new_root_depth > BRANCH_INLINE_DEPTH:
                continue
            frontier.append((target, child_depth, new_root_depth, new_ancestors))

    return steps


_QUEUE_PUBLISH_RE = re.compile(
    r"^(?:enqueue|publish|send_task|delay|apply_async)$",
)
_EXTERNAL_HTTP_RE = re.compile(
    r"^(?:requests\.(?:get|post|put|delete|patch)|httpx\.(?:get|post|put|delete|patch)|fetch|axios(?:\.\w+)?)$",
)
_DB_WRITE_RE = re.compile(
    r"^(?:session\.(?:add|delete|commit|merge)|.*INSERT.*|.*UPDATE.*|.*DELETE.*)$",
)


def classify_terminal(
    blob: RepoGraphBlob,
    last_step_node_id: str,
    entry_kind: EntryPointKind,
) -> TerminalKind:
    """Classify the terminal kind for a flow whose trace ends at *last_step_node_id*.

    Looks at the outgoing call edges of the last step's node. If any
    match a queue/http/db pattern (in that precedence), returns the
    matching kind. Otherwise: HTTP-entered flows with no outgoing call
    edges default to ``"response"``; other entry kinds default to
    ``"none"``.
    """
    nodes_by_id = {n.id: n for n in blob.nodes}
    outgoing_targets: list[Node] = []
    for edge in blob.edges:
        if edge.kind == "calls" and edge.source == last_step_node_id:
            target = nodes_by_id.get(edge.target)
            if target is not None:
                outgoing_targets.append(target)

    for target in outgoing_targets:
        if _QUEUE_PUBLISH_RE.match(target.label):
            return "queue_publish"
    for target in outgoing_targets:
        if _EXTERNAL_HTTP_RE.match(target.label):
            return "external_http"
    for target in outgoing_targets:
        if _DB_WRITE_RE.match(target.label):
            return "db_write"

    # Also check if the terminal node itself matches a pattern —
    # e.g. `session.commit` is the last node in the trace with no
    # further call edges; its own label signals the terminal kind.
    last_node = nodes_by_id.get(last_step_node_id)
    if last_node is not None:
        if _QUEUE_PUBLISH_RE.match(last_node.label):
            return "queue_publish"
        if _EXTERNAL_HTTP_RE.match(last_node.label):
            return "external_http"
        if _DB_WRITE_RE.match(last_node.label):
            return "db_write"

    if not outgoing_targets and entry_kind == "http":
        return "response"
    return "none"


# Bumped when the derivation logic changes in a way that invalidates
# persisted flow_json blobs.
DERIVER_VERSION = "phase1"


def _stable_flow_id(entry_node_id: str) -> str:
    digest = hashlib.sha256(entry_node_id.encode("utf-8")).hexdigest()
    return digest[:12]


def _hash_file_set(file_set: list[str], workspace_root: Path | None) -> str:
    hasher = hashlib.sha256()
    for path in file_set:
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        if workspace_root is not None:
            full = workspace_root / path
            try:
                data = full.read_bytes()
            except OSError:
                data = b""
            hasher.update(len(data).to_bytes(8, "big"))
            hasher.update(data)
        # When workspace_root is None we hash path-only — useful for unit
        # tests and as a deterministic fallback when no workspace is
        # available.  Phase 2 always supplies a workspace.
    return f"sha256:{hasher.hexdigest()}"


def _hash_flow_membership(flow_ids: list[str]) -> str:
    joined = ",".join(sorted(flow_ids))
    return f"sha256:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


def derive_flow_blob(
    graph_blob: RepoGraphBlob,
    workspace_root: Path | None,
) -> FlowJsonBlob:
    """Compose entry-point detection + per-entry forward trace +
    terminal classification + file-set hashing into a single
    :class:`FlowJsonBlob`.

    When *workspace_root* is provided, ``file_set_hash`` uses the live
    contents of each file in the flow's ``file_set``. Otherwise the
    hash is path-only (Phase 1 callers may run without a workspace;
    Phase 2's labelling step always supplies one).
    """
    nodes_by_id = {n.id: n for n in graph_blob.nodes}
    entry_points = detect_entry_points(graph_blob)

    flows: list[Flow] = []
    reached: set[str] = set()
    for ep in entry_points:
        steps = trace_flow(graph_blob, ep)
        if not steps:
            continue
        for step in steps:
            reached.add(step.node_id)

        file_set = sorted(
            {
                nodes_by_id[s.node_id].file
                for s in steps
                if s.node_id in nodes_by_id and nodes_by_id[s.node_id].file
            },
        )
        last_step = steps[-1]
        terminal_kind = classify_terminal(
            graph_blob,
            last_step.node_id,
            ep.kind,
        )
        flow = Flow(
            id=_stable_flow_id(ep.node_id),
            entry_point=ep,
            terminal_node_id=last_step.node_id,
            terminal_kind=terminal_kind,
            steps=steps,
            file_set=file_set,
            file_set_hash=_hash_file_set(file_set, workspace_root),
            name=None,
            description=None,
        )
        flows.append(flow)

    flow_ids = [f.id for f in flows]
    capability = Capability(
        id="unlabeled",
        flow_ids=flow_ids,
        flow_membership_hash=_hash_flow_membership(flow_ids),
        name=None,
        description=None,
    )

    unreached = sorted(
        n.id for n in graph_blob.nodes if n.id not in reached and n.kind == "function"
    )

    return FlowJsonBlob(
        capabilities=[capability],
        flows=flows,
        unreached=unreached,
        derived_at_commit=graph_blob.commit_sha,
        deriver_version=DERIVER_VERSION,
    )


__all__ = [
    "BRANCH_INLINE_DEPTH",
    "DERIVER_VERSION",
    "MAX_FLOW_STEPS",
    "classify_terminal",
    "derive_flow_blob",
    "trace_flow",
]
