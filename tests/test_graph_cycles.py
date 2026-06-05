"""Unit tests for agent.graph_analyzer.cycles.compute_cycles.

Tests use hand-built Edge/EdgeEvidence lists to cover:
- 3-module import cycle
- acyclic diamond graph
- self-import
- two disjoint 2-cycles
- 2-cycle with an external non-cycle node
- non-imports edges are ignored
- determinism across two calls
"""

from __future__ import annotations

from agent.graph_analyzer.cycles import compute_cycles
from shared.types import DependencyCycle, Edge, EdgeEvidence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev(file: str = "a.py", line: int = 1, snippet: str = "import b") -> EdgeEvidence:
    return EdgeEvidence(file=file, line=line, snippet=snippet)


def _import_edge(source: str, target: str, *, file: str | None = None, line: int = 1) -> Edge:
    """Create a minimal imports Edge."""
    return Edge(
        source=source,
        target=target,
        kind="imports",
        evidence=_ev(file=file or f"{source}.py", line=line, snippet=f"import {target}"),
        source_kind="ast",
    )


def _calls_edge(source: str, target: str) -> Edge:
    """Create a calls Edge (should be ignored by compute_cycles)."""
    return Edge(
        source=source,
        target=target,
        kind="calls",
        evidence=_ev(file=f"{source}.py", snippet=f"{target}()"),
        source_kind="ast",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestThreeModuleCycle:
    """a -> b -> c -> a forms one 3-member cycle."""

    def test_exactly_one_cycle_returned(self) -> None:
        edges = [
            _import_edge("module:a", "module:b", file="a.py", line=1),
            _import_edge("module:b", "module:c", file="b.py", line=2),
            _import_edge("module:c", "module:a", file="c.py", line=3),
        ]
        cycles = compute_cycles(edges)
        assert len(cycles) == 1

    def test_members_are_sorted_and_complete(self) -> None:
        edges = [
            _import_edge("module:a", "module:b"),
            _import_edge("module:b", "module:c"),
            _import_edge("module:c", "module:a"),
        ]
        cycles = compute_cycles(edges)
        assert cycles[0].members == sorted(["module:a", "module:b", "module:c"])

    def test_kind_is_import(self) -> None:
        edges = [
            _import_edge("module:a", "module:b"),
            _import_edge("module:b", "module:c"),
            _import_edge("module:c", "module:a"),
        ]
        cycles = compute_cycles(edges)
        assert cycles[0].kind == "import"

    def test_three_closing_edges(self) -> None:
        edges = [
            _import_edge("module:a", "module:b", file="a.py", line=1),
            _import_edge("module:b", "module:c", file="b.py", line=2),
            _import_edge("module:c", "module:a", file="c.py", line=3),
        ]
        cycles = compute_cycles(edges)
        assert len(cycles[0].closing_edges) == 3

    def test_stable_id_format(self) -> None:
        """id must be cycle:<sorted_members_joined_with_pipe>."""
        edges = [
            _import_edge("module:a", "module:b"),
            _import_edge("module:b", "module:c"),
            _import_edge("module:c", "module:a"),
        ]
        cycles = compute_cycles(edges)
        expected_id = "cycle:module:a|module:b|module:c"
        assert cycles[0].id == expected_id

    def test_is_dependency_cycle_instance(self) -> None:
        edges = [
            _import_edge("module:a", "module:b"),
            _import_edge("module:b", "module:c"),
            _import_edge("module:c", "module:a"),
        ]
        cycles = compute_cycles(edges)
        assert isinstance(cycles[0], DependencyCycle)


class TestAcyclicDiamond:
    """a -> b, a -> c, b -> d, c -> d — no cycles."""

    def test_no_cycles(self) -> None:
        edges = [
            _import_edge("module:a", "module:b"),
            _import_edge("module:a", "module:c"),
            _import_edge("module:b", "module:d"),
            _import_edge("module:c", "module:d"),
        ]
        cycles = compute_cycles(edges)
        assert cycles == []


class TestSelfImport:
    """a -> a is a self-loop and must be reported as a 1-member cycle."""

    def test_one_cycle_with_single_member(self) -> None:
        edges = [_import_edge("module:a", "module:a", file="a.py", line=5)]
        cycles = compute_cycles(edges)
        assert len(cycles) == 1
        assert cycles[0].members == ["module:a"]

    def test_self_loop_closing_edge_included(self) -> None:
        edges = [_import_edge("module:a", "module:a", file="a.py", line=5)]
        cycles = compute_cycles(edges)
        assert len(cycles[0].closing_edges) == 1
        assert cycles[0].closing_edges[0].file == "a.py"
        assert cycles[0].closing_edges[0].line == 5

    def test_self_loop_id(self) -> None:
        edges = [_import_edge("module:a", "module:a")]
        cycles = compute_cycles(edges)
        assert cycles[0].id == "cycle:module:a"


class TestTwoDisjointTwoCycles:
    """a <-> b and c <-> d — two independent 2-cycles."""

    def test_two_cycles_returned(self) -> None:
        edges = [
            _import_edge("module:a", "module:b", file="a.py", line=1),
            _import_edge("module:b", "module:a", file="b.py", line=1),
            _import_edge("module:c", "module:d", file="c.py", line=1),
            _import_edge("module:d", "module:c", file="d.py", line=1),
        ]
        cycles = compute_cycles(edges)
        assert len(cycles) == 2

    def test_no_duplicates(self) -> None:
        edges = [
            _import_edge("module:a", "module:b"),
            _import_edge("module:b", "module:a"),
            _import_edge("module:c", "module:d"),
            _import_edge("module:d", "module:c"),
        ]
        cycles = compute_cycles(edges)
        ids = [c.id for c in cycles]
        assert len(ids) == len(set(ids))

    def test_correct_members_for_each_cycle(self) -> None:
        edges = [
            _import_edge("module:a", "module:b"),
            _import_edge("module:b", "module:a"),
            _import_edge("module:c", "module:d"),
            _import_edge("module:d", "module:c"),
        ]
        cycles = compute_cycles(edges)
        member_sets = {frozenset(c.members) for c in cycles}
        assert frozenset(["module:a", "module:b"]) in member_sets
        assert frozenset(["module:c", "module:d"]) in member_sets


class TestTwoCycleWithExternalNode:
    """a <-> b plus x -> a: cycle must be a and b only; x -> a not in closing_edges."""

    def test_one_cycle_with_only_ab(self) -> None:
        edges = [
            _import_edge("module:a", "module:b", file="a.py", line=1),
            _import_edge("module:b", "module:a", file="b.py", line=1),
            _import_edge("module:x", "module:a", file="x.py", line=1),
        ]
        cycles = compute_cycles(edges)
        assert len(cycles) == 1
        assert sorted(cycles[0].members) == ["module:a", "module:b"]

    def test_external_edge_excluded_from_closing_edges(self) -> None:
        edges = [
            _import_edge("module:a", "module:b", file="a.py", line=1),
            _import_edge("module:b", "module:a", file="b.py", line=1),
            _import_edge("module:x", "module:a", file="x.py", line=1),
        ]
        cycles = compute_cycles(edges)
        for ev in cycles[0].closing_edges:
            # x.py must not appear — it is not part of the cycle SCC
            assert ev.file != "x.py", "x->a edge must not appear in closing_edges"

    def test_two_closing_edges_for_bidirectional_cycle(self) -> None:
        edges = [
            _import_edge("module:a", "module:b", file="a.py", line=1),
            _import_edge("module:b", "module:a", file="b.py", line=1),
            _import_edge("module:x", "module:a", file="x.py", line=1),
        ]
        cycles = compute_cycles(edges)
        assert len(cycles[0].closing_edges) == 2


class TestNonImportsEdgesIgnored:
    """Calls edges forming a loop must produce zero cycles."""

    def test_calls_loop_ignored(self) -> None:
        edges = [
            _calls_edge("module:a", "module:b"),
            _calls_edge("module:b", "module:a"),
        ]
        cycles = compute_cycles(edges)
        assert cycles == []

    def test_mixed_only_imports_counted(self) -> None:
        """A calls loop alongside an acyclic imports graph → zero cycles."""
        edges = [
            _calls_edge("module:a", "module:b"),
            _calls_edge("module:b", "module:a"),
            _import_edge("module:a", "module:b"),  # one-way imports only
        ]
        cycles = compute_cycles(edges)
        assert cycles == []


class TestDeterminism:
    """compute_cycles must return identical results across two calls."""

    def test_ids_are_identical_across_two_calls(self) -> None:
        edges = [
            _import_edge("module:a", "module:b"),
            _import_edge("module:b", "module:c"),
            _import_edge("module:c", "module:a"),
            _import_edge("module:x", "module:y"),
            _import_edge("module:y", "module:x"),
        ]
        first = compute_cycles(edges)
        second = compute_cycles(edges)
        assert [c.id for c in first] == [c.id for c in second]

    def test_member_order_is_sorted_and_stable(self) -> None:
        edges = [
            _import_edge("module:z", "module:a"),
            _import_edge("module:a", "module:m"),
            _import_edge("module:m", "module:z"),
        ]
        first = compute_cycles(edges)
        second = compute_cycles(edges)
        assert first[0].members == second[0].members
        # Also confirm members are sorted
        assert first[0].members == sorted(first[0].members)

    def test_closing_edges_sorted_by_file_line(self) -> None:
        """closing_edges must be sorted deterministically (file, line)."""
        edges = [
            _import_edge("module:a", "module:b", file="z_file.py", line=10),
            _import_edge("module:b", "module:a", file="a_file.py", line=5),
        ]
        cycles = compute_cycles(edges)
        closing = cycles[0].closing_edges
        assert len(closing) == 2
        # First by file (a_file.py < z_file.py)
        assert closing[0].file == "a_file.py"
        assert closing[1].file == "z_file.py"

    def test_cycles_list_sorted_by_id(self) -> None:
        """Returned list must be sorted by cycle id."""
        edges = [
            _import_edge("module:z", "module:y"),
            _import_edge("module:y", "module:z"),
            _import_edge("module:a", "module:b"),
            _import_edge("module:b", "module:a"),
        ]
        cycles = compute_cycles(edges)
        ids = [c.id for c in cycles]
        assert ids == sorted(ids)


class TestParallelEdges:
    """Parallel edges (same source+target, different lines) must each produce
    exactly one evidence entry — no N² explosion."""

    def test_parallel_edges_one_evidence_each(self) -> None:
        """a->b at line 1, a->b at line 20 (parallel), b->a at line 1.

        Expect: exactly one cycle, members == ["a", "b"],
        and len(closing_edges) == 3 (one per import edge), NOT 5.
        """
        edges = [
            _import_edge("a", "b", file="a.py", line=1),
            _import_edge("a", "b", file="a.py", line=20),
            _import_edge("b", "a", file="b.py", line=1),
        ]
        cycles = compute_cycles(edges)
        assert len(cycles) == 1
        assert cycles[0].members == ["a", "b"]
        assert len(cycles[0].closing_edges) == 3, (
            f"Expected 3 closing_edges (one per edge), got {len(cycles[0].closing_edges)}"
        )

    def test_parallel_edges_evidence_sorted(self) -> None:
        """closing_edges for parallel edges are sorted by (file, line)."""
        edges = [
            _import_edge("a", "b", file="a.py", line=20),
            _import_edge("a", "b", file="a.py", line=1),
            _import_edge("b", "a", file="b.py", line=1),
        ]
        cycles = compute_cycles(edges)
        closing = cycles[0].closing_edges
        assert closing[0].file == "a.py" and closing[0].line == 1
        assert closing[1].file == "a.py" and closing[1].line == 20
        assert closing[2].file == "b.py" and closing[2].line == 1


class TestEmptyInput:
    """Edge cases: empty lists and single-node graphs."""

    def test_empty_edges(self) -> None:
        assert compute_cycles([]) == []

    def test_single_acyclic_node(self) -> None:
        edges = [_import_edge("module:a", "module:b")]
        assert compute_cycles(edges) == []
