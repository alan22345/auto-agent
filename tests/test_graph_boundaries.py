"""Tests for ``agent/graph_analyzer/boundaries.py`` (ADR-016 Phase 5 §7).

Covers:

* ``flag_violations`` — internal-access flagging, explicit-rule precedence,
  http exemption, same-area exemption, area/file-target exemption.
* ``load_boundary_rules`` — YAML parsing, absent file, malformed file,
  forward-compat (unknown keys ignored).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.graph_analyzer.boundaries import (
    BoundaryRule,
    flag_violations,
    load_boundary_rules,
)
from shared.types import Edge, EdgeEvidence, Node

if TYPE_CHECKING:
    from pathlib import Path


def _node(id_: str, *, area: str, kind: str = "function") -> Node:
    return Node(
        id=id_,
        kind=kind,
        label=id_.rsplit(":", 1)[-1],
        file=None,
        line_start=None,
        line_end=None,
        area=area,
        parent=None,
    )


def _edge(
    source: str,
    target: str,
    *,
    kind: str = "calls",
    source_kind: str = "ast",
) -> Edge:
    return Edge(
        source=source,
        target=target,
        kind=kind,
        evidence=EdgeEvidence(file="x.py", line=1, snippet="x"),
        source_kind=source_kind,
    )


class TestInternalAccess:
    def test_cross_area_edge_to_private_target_is_flagged(self) -> None:
        nodes = [
            _node("a/foo.py::caller", area="area_a"),
            _node("b/bar.py::_private", area="area_b"),
        ]
        edges = [_edge("a/foo.py::caller", "b/bar.py::_private")]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols=set(),
            rules=[],
        )
        assert flagged[0].boundary_violation is True
        assert flagged[0].violation_reason == "internal_access"

    def test_cross_area_edge_to_public_target_is_not_flagged(self) -> None:
        nodes = [
            _node("a/foo.py::caller", area="area_a"),
            _node("b/bar.py::PublicThing", area="area_b", kind="class"),
        ]
        edges = [_edge("a/foo.py::caller", "b/bar.py::PublicThing")]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols={"b/bar.py::PublicThing"},
            rules=[],
        )
        assert flagged[0].boundary_violation is False
        assert flagged[0].violation_reason is None

    def test_same_area_edge_is_never_flagged(self) -> None:
        nodes = [
            _node("a/foo.py::caller", area="area_a"),
            _node("a/bar.py::_private", area="area_a"),
        ]
        edges = [_edge("a/foo.py::caller", "a/bar.py::_private")]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols=set(),
            rules=[],
        )
        assert flagged[0].boundary_violation is False
        assert flagged[0].violation_reason is None

    def test_edge_into_file_node_is_never_flagged(self) -> None:
        # imports edges typically target ``file:`` or ``module:`` nodes
        # which are not class/function — the spec exempts these.
        nodes = [
            _node("a/foo.py::caller", area="area_a"),
            _node("file:b/bar.py", area="area_b", kind="file"),
        ]
        edges = [_edge("a/foo.py::caller", "file:b/bar.py", kind="imports")]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols=set(),
            rules=[],
        )
        assert flagged[0].boundary_violation is False

    def test_edge_to_area_node_is_never_flagged(self) -> None:
        nodes = [
            _node("a/foo.py::caller", area="area_a"),
            _node("area:area_b", area="area_b", kind="area"),
        ]
        edges = [_edge("a/foo.py::caller", "area:area_b")]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols=set(),
            rules=[],
        )
        assert flagged[0].boundary_violation is False

    def test_edge_with_missing_target_node_is_passed_through(self) -> None:
        # A target id without a corresponding Node (e.g. ``module:react``
        # for a TS external import) cannot be classified — the flagger
        # must not crash and must not flag it.
        nodes = [_node("a/foo.py::caller", area="area_a")]
        edges = [_edge("a/foo.py::caller", "module:react", kind="imports")]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols=set(),
            rules=[],
        )
        assert flagged[0].boundary_violation is False
        assert flagged[0].violation_reason is None


class TestHttpExemption:
    def test_http_edge_to_private_target_is_not_flagged(self) -> None:
        # HTTP edges are intentional cross-language pattern — never a
        # layering breach even when the target is private by convention.
        nodes = [
            _node("ts/client.ts::caller", area="frontend"),
            _node("api/router.py::_secret_route", area="backend"),
        ]
        edges = [
            _edge(
                "ts/client.ts::caller",
                "api/router.py::_secret_route",
                kind="http",
            ),
        ]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols=set(),
            rules=[],
        )
        assert flagged[0].boundary_violation is False
        assert flagged[0].violation_reason is None

    def test_http_edge_is_not_flagged_even_with_explicit_rule(self) -> None:
        # HTTP exemption is unconditional. An explicit rule from->to
        # matching the edge's areas must NOT fire.
        nodes = [
            _node("ts/client.ts::caller", area="frontend"),
            _node("api/router.py::route", area="backend"),
        ]
        edges = [
            _edge("ts/client.ts::caller", "api/router.py::route", kind="http"),
        ]
        rules = [BoundaryRule(index=0, from_area="frontend", to_areas=("backend",))]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols=set(),
            rules=rules,
        )
        assert flagged[0].boundary_violation is False


class TestExplicitRule:
    def test_explicit_rule_flags_edge_with_indexed_reason(self) -> None:
        nodes = [
            _node("shared/x.py::foo", area="shared"),
            _node("agent/y.py::Bar", area="agent", kind="class"),
        ]
        edges = [_edge("shared/x.py::foo", "agent/y.py::Bar")]
        rules = [BoundaryRule(index=0, from_area="shared", to_areas=("agent",))]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols={"agent/y.py::Bar"},
            rules=rules,
        )
        assert flagged[0].boundary_violation is True
        assert flagged[0].violation_reason == "explicit_rule:0"

    def test_explicit_rule_wins_over_internal_access(self) -> None:
        # When both checks would fire, the explicit rule must be reported.
        nodes = [
            _node("shared/x.py::foo", area="shared"),
            _node("agent/y.py::_private", area="agent"),
        ]
        edges = [_edge("shared/x.py::foo", "agent/y.py::_private")]
        rules = [BoundaryRule(index=3, from_area="shared", to_areas=("agent",))]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols=set(),
            rules=rules,
        )
        assert flagged[0].boundary_violation is True
        assert flagged[0].violation_reason == "explicit_rule:3"

    def test_first_matching_rule_wins(self) -> None:
        nodes = [
            _node("shared/x.py::foo", area="shared"),
            _node("agent/y.py::Bar", area="agent", kind="class"),
        ]
        edges = [_edge("shared/x.py::foo", "agent/y.py::Bar")]
        rules = [
            BoundaryRule(
                index=0,
                from_area="shared",
                to_areas=("agent", "orchestrator"),
            ),
            BoundaryRule(index=1, from_area="shared", to_areas=("agent",)),
        ]
        flagged = flag_violations(
            edges=edges,
            nodes=nodes,
            public_symbols={"agent/y.py::Bar"},
            rules=rules,
        )
        assert flagged[0].violation_reason == "explicit_rule:0"


class TestLoadBoundaryRules:
    def test_absent_yaml_returns_empty(self, tmp_path: Path) -> None:
        assert load_boundary_rules(str(tmp_path)) == []

    def test_yaml_without_boundaries_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".auto-agent").mkdir()
        (tmp_path / ".auto-agent" / "graph.yml").write_text(
            "areas:\n  - name: foo\n    paths: ['foo/**']\n",
        )
        assert load_boundary_rules(str(tmp_path)) == []

    def test_yaml_with_forbid_rules_parses(self, tmp_path: Path) -> None:
        (tmp_path / ".auto-agent").mkdir()
        (tmp_path / ".auto-agent" / "graph.yml").write_text(
            "boundaries:\n"
            "  - forbid:\n"
            "      from: shared\n"
            "      to: [agent, orchestrator]\n"
            "  - forbid:\n"
            "      from: agent\n"
            "      to: [web]\n",
        )
        rules = load_boundary_rules(str(tmp_path))
        assert len(rules) == 2
        assert rules[0].index == 0
        assert rules[0].from_area == "shared"
        assert rules[0].to_areas == ("agent", "orchestrator")
        assert rules[1].index == 1
        assert rules[1].from_area == "agent"
        assert rules[1].to_areas == ("web",)

    def test_yaml_with_malformed_entries_skips_them(self, tmp_path: Path) -> None:
        (tmp_path / ".auto-agent").mkdir()
        (tmp_path / ".auto-agent" / "graph.yml").write_text(
            "boundaries:\n"
            "  - not_a_forbid: 1\n"
            "  - forbid:\n"
            "      from: ''\n"
            "      to: [agent]\n"
            "  - forbid:\n"
            "      from: shared\n"
            "      to: []\n"
            "  - forbid:\n"
            "      from: shared\n"
            "      to: [agent]\n",
        )
        rules = load_boundary_rules(str(tmp_path))
        assert len(rules) == 1
        # Index preserves the original YAML position (3 here), so users can
        # cross-reference rule numbers between the file and the UI.
        assert rules[0].index == 3
        assert rules[0].from_area == "shared"
        assert rules[0].to_areas == ("agent",)

    def test_malformed_yaml_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".auto-agent").mkdir()
        (tmp_path / ".auto-agent" / "graph.yml").write_text(
            "boundaries: [\n",  # broken YAML
        )
        assert load_boundary_rules(str(tmp_path)) == []
