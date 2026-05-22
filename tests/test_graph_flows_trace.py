"""Forward-trace tests for flow derivation (Phase 1).

trace_flow walks call edges forward from an entry point, capping depth
and collapsing deep branches per spec §3 steps 3-4.
"""
from __future__ import annotations

from datetime import UTC, datetime

from agent.graph_analyzer.flows import MAX_FLOW_STEPS, trace_flow
from shared.types import (
    Edge,
    EdgeEvidence,
    EntryPoint,
    Node,
    RepoGraphBlob,
)


def _blob(nodes, edges):
    return RepoGraphBlob(
        commit_sha="0" * 40,
        generated_at=datetime.now(tz=UTC),
        analyser_version="test",
        areas=[],
        nodes=nodes,
        edges=edges,
    )


def _fn(node_id: str) -> Node:
    return Node(
        id=node_id, kind="function", label=node_id, file=f"{node_id}.py", area="src",
    )


def _call(src: str, dst: str) -> Edge:
    return Edge(
        source=src,
        target=dst,
        kind="calls",
        evidence=EdgeEvidence(file=f"{src}.py", line=1, snippet=f"{dst}()"),
        source_kind="ast",
    )


def test_linear_chain():
    blob = _blob(
        [_fn("a"), _fn("b"), _fn("c")],
        [_call("a", "b"), _call("b", "c")],
    )
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    assert [s.node_id for s in steps] == ["a", "b", "c"]
    assert [s.depth for s in steps] == [0, 1, 2]
    assert all(not s.is_branch_root for s in steps)
    assert all(not s.is_cycle_back for s in steps)


def test_branch_marks_root_and_inlines_both_branches():
    blob = _blob(
        [_fn("a"), _fn("b"), _fn("c"), _fn("d")],
        [_call("a", "b"), _call("a", "c"), _call("c", "d")],
    )
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    by_id = {s.node_id: s for s in steps}
    assert by_id["a"].is_branch_root is True
    assert by_id["b"].depth == 1
    assert by_id["c"].depth == 1
    assert by_id["d"].depth == 2


def test_cycle_back_edge_is_terminal_not_expanded():
    blob = _blob(
        [_fn("a"), _fn("b")],
        [_call("a", "b"), _call("b", "a")],
    )
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    ids = [s.node_id for s in steps]
    # "a" appears twice: at depth 0 (root) and at depth 2 (cycle-back).
    assert ids == ["a", "b", "a"]
    assert steps[-1].is_cycle_back is True


def test_branch_depth_capped_at_three_past_branch_root():
    # Build: a→b, a→c, c→c1→c2→c3→c4 (c4 is past the depth-3 cap)
    nodes = [_fn(x) for x in ("a", "b", "c", "c1", "c2", "c3", "c4")]
    edges = [
        _call("a", "b"),
        _call("a", "c"),
        _call("c", "c1"),
        _call("c1", "c2"),
        _call("c2", "c3"),
        _call("c3", "c4"),
    ]
    blob = _blob(nodes, edges)
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    ids = [s.node_id for s in steps]
    # branch root is "a", branch via "c" extends c→c1→c2→c3 (depth-3
    # past the branch root at depth 1) — c4 (depth 5) is dropped.
    assert "c4" not in ids
    assert "c3" in ids


def test_max_flow_steps_cap():
    # A chain longer than MAX_FLOW_STEPS terminates at MAX_FLOW_STEPS.
    n = MAX_FLOW_STEPS + 5
    nodes = [_fn(f"n{i}") for i in range(n)]
    edges = [_call(f"n{i}", f"n{i + 1}") for i in range(n - 1)]
    blob = _blob(nodes, edges)
    steps = trace_flow(blob, EntryPoint(node_id="n0", kind="http"))
    assert len(steps) == MAX_FLOW_STEPS


def test_non_call_edges_are_not_followed():
    blob = _blob(
        [_fn("a"), _fn("b"), _fn("c")],
        [
            _call("a", "b"),
            # 'imports' should NOT be traversed
            Edge(
                source="a",
                target="c",
                kind="imports",
                evidence=EdgeEvidence(file="a.py", line=1, snippet="import c"),
                source_kind="ast",
            ),
        ],
    )
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    assert [s.node_id for s in steps] == ["a", "b"]
