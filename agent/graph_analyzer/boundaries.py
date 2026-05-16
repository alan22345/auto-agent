"""Boundary-violation flagging (ADR-016 Phase 5 §7).

Two rules combined:

1. **Convention-based public surface.** Every parser exposes a
   ``ParseResult.public_symbols`` set listing the node ids it considers
   public for cross-area consumption. A cross-area edge whose target is
   a class- or function-kind node *not* in that set is flagged as an
   internal-access violation.

2. **Explicit ``.auto-agent/graph.yml`` rules.** Optional ``boundaries``
   list with ``forbid`` entries (``{from: <area>, to: [<area>, ...]}``).
   An edge whose source-area matches ``from`` and target-area matches any
   ``to`` is flagged as an explicit-rule violation — *regardless* of
   whether the target is in the public surface. Explicit rules take
   precedence: the reason recorded is ``"explicit_rule:<index>"`` with
   the 0-based position of the rule in the YAML file.

Exemptions (never flagged):

* HTTP edges (``kind="http"``) — intentional cross-language pattern.
* Edges into area / file nodes — only fine-grained call / import /
  inherit / inherits edges through class- or function-kind nodes
  qualify.
* Same-area edges — public/private is a cross-area concept only.

The module is pure: no I/O outside ``load_boundary_rules`` (which reads
``.auto-agent/graph.yml`` once) and no LLM calls. The pipeline calls
:func:`flag_violations` after HTTP matching is complete, replacing the
edge list in-place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
import yaml

if TYPE_CHECKING:
    from shared.types import Edge, Node

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class BoundaryRule:
    """One ``forbid`` entry from ``.auto-agent/graph.yml::boundaries``.

    ``index`` is the 0-based position in the YAML file's ``boundaries``
    list; the pipeline writes it back to ``Edge.violation_reason`` so a
    UI can map a flagged edge to a specific rule.
    """

    index: int
    from_area: str
    to_areas: tuple[str, ...]

    def applies(self, source_area: str, target_area: str) -> bool:
        """True iff a cross-area edge with the given areas is forbidden."""
        return source_area == self.from_area and target_area in self.to_areas


def load_boundary_rules(workspace: str) -> list[BoundaryRule]:
    """Read ``.auto-agent/graph.yml`` and return its ``boundaries`` rules.

    Returns an empty list when:

    * the file is absent,
    * the file is present but has no ``boundaries`` key,
    * the file is present but ``boundaries`` is empty / not a list,
    * parsing the YAML raises (logged + returned as empty so callers
      keep working when the file is malformed).

    Each rule entry must match the shape::

        - forbid:
            from: <area>
            to: [<area>, ...]

    Unknown keys / wrong shapes are silently dropped from the rule list
    (forward compatibility — adding new boundary kinds later won't crash
    older binaries).
    """
    path = os.path.join(workspace, ".auto-agent", "graph.yml")
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        log.warning(
            "graph_boundary_rules_parse_failed",
            workspace=workspace,
            error=str(e),
            error_type=e.__class__.__name__,
        )
        return []
    raw = data.get("boundaries")
    if not isinstance(raw, list):
        return []
    rules: list[BoundaryRule] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        forbid = entry.get("forbid")
        if not isinstance(forbid, dict):
            continue
        from_area = forbid.get("from")
        to_areas = forbid.get("to")
        if not isinstance(from_area, str) or not from_area:
            continue
        if not isinstance(to_areas, list):
            continue
        normalised_to = tuple(a for a in to_areas if isinstance(a, str) and a)
        if not normalised_to:
            continue
        rules.append(
            BoundaryRule(
                index=i,
                from_area=from_area,
                to_areas=normalised_to,
            ),
        )
    return rules


def flag_violations(
    *,
    edges: list[Edge],
    nodes: list[Node],
    public_symbols: set[str],
    rules: list[BoundaryRule],
) -> list[Edge]:
    """Return a new list of edges with violation fields populated.

    The function never mutates its inputs. Edges with ``kind="http"``
    are passed through unflagged regardless of any other condition.
    Edges into area- or file-kind nodes are likewise never flagged —
    only fine-grained call/import/inherit edges to class/function nodes
    are subject to the public-surface check.

    Precedence: when both an explicit rule and an internal-access rule
    would fire on the same edge, the explicit rule wins. The reason
    recorded is ``"explicit_rule:<index>"``.
    """
    from shared.types import Edge  # local — keep types module out of import graph

    # Index nodes by id once so we can look up area/kind in O(1).
    node_by_id: dict[str, Node] = {n.id: n for n in nodes}

    out: list[Edge] = []
    for edge in edges:
        source_node = node_by_id.get(edge.source)
        target_node = node_by_id.get(edge.target)
        boundary_violation = False
        violation_reason: str | None = None

        if (
            edge.kind != "http"
            and source_node is not None
            and target_node is not None
            and source_node.area != target_node.area
        ):
            # First — explicit rule check (wins on precedence).
            for rule in rules:
                if rule.applies(source_node.area, target_node.area):
                    boundary_violation = True
                    violation_reason = f"explicit_rule:{rule.index}"
                    break

            # Then — public-surface check (only if no explicit rule fired
            # and only when the target is fine-grained).
            if (
                not boundary_violation
                and target_node.kind in ("class", "function")
                and target_node.id not in public_symbols
            ):
                boundary_violation = True
                violation_reason = "internal_access"

        out.append(
            Edge(
                source=edge.source,
                target=edge.target,
                kind=edge.kind,
                evidence=edge.evidence,
                source_kind=edge.source_kind,
                boundary_violation=boundary_violation,
                violation_reason=violation_reason,
            ),
        )
    return out


__all__ = ["BoundaryRule", "flag_violations", "load_boundary_rules"]
