"""Tests for the DependencyCycle schema and RepoGraphBlob.cycles field.

TDD: written before the schema exists to drive the additions in
shared/types.py (Task A of §3 cycle-detection feature, ADR-016 phase 9).
"""

from datetime import UTC, datetime

import pytest

from shared.types import (
    DependencyCycle,
    EdgeEvidence,
    RepoGraphBlob,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_edge_evidence(**overrides) -> dict:
    base = {"file": "agent/a.py", "line": 5, "snippet": "import agent.b"}
    base.update(overrides)
    return base


def _minimal_repo_graph_blob(**overrides) -> dict:
    """Return the minimum dict needed to construct a RepoGraphBlob."""
    base = {
        "commit_sha": "abc123",
        "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "analyser_version": "phase9-cycles-0.9.0",
        "areas": [],
        "nodes": [],
        "edges": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# DependencyCycle construction and round-trip
# ---------------------------------------------------------------------------


class TestDependencyCycleConstruction:
    """DependencyCycle must construct from valid input."""

    def test_basic_construction(self):
        ev = EdgeEvidence(**_minimal_edge_evidence())
        cycle = DependencyCycle(
            id="cycle:0",
            kind="import",
            members=["module:agent.a", "module:agent.b"],
            closing_edges=[ev],
        )
        assert cycle.id == "cycle:0"
        assert cycle.kind == "import"
        assert cycle.members == ["module:agent.a", "module:agent.b"]
        assert len(cycle.closing_edges) == 1
        assert cycle.closing_edges[0].snippet == "import agent.b"

    def test_call_kind_accepted(self):
        ev = EdgeEvidence(**_minimal_edge_evidence())
        cycle = DependencyCycle(
            id="cycle:1",
            kind="call",
            members=["module:agent.x"],
            closing_edges=[ev],
        )
        assert cycle.kind == "call"

    def test_invalid_kind_rejected(self):
        ev = EdgeEvidence(**_minimal_edge_evidence())
        with pytest.raises(ValueError):
            DependencyCycle(
                id="cycle:bad",
                kind="unknown",  # not a valid Literal
                members=["module:agent.a"],
                closing_edges=[ev],
            )


class TestDependencyCycleRoundTrip:
    """DependencyCycle must survive model_dump() / model_validate() round-trip."""

    def test_round_trip(self):
        ev = EdgeEvidence(**_minimal_edge_evidence())
        original = DependencyCycle(
            id="cycle:0",
            kind="import",
            members=["module:agent.a", "module:agent.b"],
            closing_edges=[ev],
        )
        dumped = original.model_dump()
        restored = DependencyCycle.model_validate(dumped)
        assert restored.id == original.id
        assert restored.kind == original.kind
        assert restored.members == original.members
        assert restored.closing_edges[0].file == ev.file
        assert restored.closing_edges[0].line == ev.line
        assert restored.closing_edges[0].snippet == ev.snippet

    def test_empty_members_allowed(self):
        """An empty members list is unusual but not schema-invalid."""
        ev = EdgeEvidence(**_minimal_edge_evidence())
        cycle = DependencyCycle(id="cycle:empty", kind="import", members=[], closing_edges=[ev])
        dumped = cycle.model_dump()
        restored = DependencyCycle.model_validate(dumped)
        assert restored.members == []

    def test_multiple_closing_edges(self):
        ev1 = EdgeEvidence(file="agent/a.py", line=5, snippet="import agent.b")
        ev2 = EdgeEvidence(file="agent/b.py", line=3, snippet="import agent.a")
        cycle = DependencyCycle(
            id="cycle:multi",
            kind="import",
            members=["module:agent.a", "module:agent.b"],
            closing_edges=[ev1, ev2],
        )
        dumped = cycle.model_dump()
        restored = DependencyCycle.model_validate(dumped)
        assert len(restored.closing_edges) == 2


# ---------------------------------------------------------------------------
# RepoGraphBlob.cycles field
# ---------------------------------------------------------------------------


class TestRepoGraphBlobCyclesField:
    """cycles field on RepoGraphBlob must default to [] and accept cycles."""

    def test_cycles_defaults_to_empty_list(self):
        blob = RepoGraphBlob(**_minimal_repo_graph_blob())
        assert blob.cycles == []

    def test_old_blob_without_cycles_key_validates_to_empty(self):
        """Backward-compat: a persisted dict without the cycles key must
        deserialize to [] rather than raising a validation error."""
        data = _minimal_repo_graph_blob()
        # Explicitly ensure the key is absent (it should be already, but be explicit)
        data.pop("cycles", None)
        blob = RepoGraphBlob.model_validate(data)
        assert blob.cycles == []

    def test_cycles_populated_survives_round_trip(self):
        ev = EdgeEvidence(**_minimal_edge_evidence())
        cycle = DependencyCycle(
            id="cycle:0",
            kind="import",
            members=["module:agent.a", "module:agent.b"],
            closing_edges=[ev],
        )
        blob = RepoGraphBlob(**_minimal_repo_graph_blob(cycles=[cycle]))
        dumped = blob.model_dump()
        restored = RepoGraphBlob.model_validate(dumped)
        assert len(restored.cycles) == 1
        assert restored.cycles[0].id == "cycle:0"
        assert restored.cycles[0].kind == "import"

    def test_cycles_field_present_in_model_dump(self):
        blob = RepoGraphBlob(**_minimal_repo_graph_blob())
        dumped = blob.model_dump()
        assert "cycles" in dumped
        assert dumped["cycles"] == []
