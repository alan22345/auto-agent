"""Tests for the CloneInstance/CloneGroup schema and RepoGraphBlob.clones field.

TDD: written before the schema exists to drive the additions in
shared/types.py (Task A of §2 duplication feature, ADR-016 phase 11).
"""

from datetime import UTC, datetime

import pydantic
import pytest

from shared.types import (
    CloneGroup,
    CloneInstance,
    RepoGraphBlob,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_clone_instance(node_id: str = "fn:a.py::helper", file: str = "a.py") -> CloneInstance:
    return CloneInstance(node_id=node_id, file=file, line_start=10, line_end=30)


def _minimal_repo_graph_blob(**overrides) -> dict:
    """Return the minimum dict needed to construct a RepoGraphBlob."""
    base = {
        "commit_sha": "abc123",
        "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "analyser_version": "phase11-duplication-0.11.0",
        "areas": [],
        "nodes": [],
        "edges": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# CloneInstance construction
# ---------------------------------------------------------------------------


class TestCloneInstanceConstruction:
    """CloneInstance must construct from valid input and round-trip cleanly."""

    def test_basic_construction(self):
        inst = CloneInstance(node_id="fn:a.py::helper", file="a.py", line_start=10, line_end=30)
        assert inst.node_id == "fn:a.py::helper"
        assert inst.file == "a.py"
        assert inst.line_start == 10
        assert inst.line_end == 30

    def test_round_trip(self):
        original = CloneInstance(node_id="fn:b.py::util", file="b.py", line_start=5, line_end=25)
        dumped = original.model_dump()
        restored = CloneInstance.model_validate(dumped)
        assert restored.node_id == original.node_id
        assert restored.file == original.file
        assert restored.line_start == original.line_start
        assert restored.line_end == original.line_end


# ---------------------------------------------------------------------------
# CloneGroup construction and round-trip
# ---------------------------------------------------------------------------


class TestCloneGroupConstruction:
    """CloneGroup must construct from valid input and round-trip cleanly."""

    def test_basic_construction(self):
        group = CloneGroup(
            id="cg-001",
            token_len=50,
            mode="strict",
            instances=[
                _minimal_clone_instance("fn:a.py::helper", "a.py"),
                _minimal_clone_instance("fn:b.py::helper", "b.py"),
            ],
        )
        assert group.id == "cg-001"
        assert group.token_len == 50
        assert group.mode == "strict"
        assert len(group.instances) == 2
        assert group.family_id is None

    def test_family_id_defaults_to_none(self):
        group = CloneGroup(
            id="cg-002",
            token_len=30,
            mode="mild",
            instances=[
                _minimal_clone_instance("fn:c.py::f", "c.py"),
                _minimal_clone_instance("fn:d.py::f", "d.py"),
            ],
        )
        assert group.family_id is None

    def test_family_id_can_be_set(self):
        group = CloneGroup(
            id="cg-003",
            token_len=40,
            mode="weak",
            instances=[
                _minimal_clone_instance("fn:e.py::g", "e.py"),
                _minimal_clone_instance("fn:f.py::g", "f.py"),
            ],
            family_id="fam-001",
        )
        assert group.family_id == "fam-001"

    def test_all_modes_accepted(self):
        for mode in ("strict", "mild", "weak", "semantic"):
            group = CloneGroup(
                id=f"cg-{mode}",
                token_len=20,
                mode=mode,
                instances=[
                    _minimal_clone_instance("fn:x.py::h", "x.py"),
                    _minimal_clone_instance("fn:y.py::h", "y.py"),
                ],
            )
            assert group.mode == mode

    def test_invalid_mode_raises_validation_error(self):
        with pytest.raises(pydantic.ValidationError):
            CloneGroup(
                id="cg-bad",
                token_len=10,
                mode="bogus",
                instances=[
                    _minimal_clone_instance("fn:a.py::f", "a.py"),
                    _minimal_clone_instance("fn:b.py::f", "b.py"),
                ],
            )

    def test_round_trip_model_dump_model_validate(self):
        original = CloneGroup(
            id="cg-rt",
            token_len=50,
            mode="strict",
            instances=[
                CloneInstance(node_id="fn:a.py::helper", file="a.py", line_start=10, line_end=30),
                CloneInstance(node_id="fn:b.py::helper", file="b.py", line_start=5, line_end=25),
            ],
            family_id=None,
        )
        dumped = original.model_dump()
        restored = CloneGroup.model_validate(dumped)
        assert restored.id == original.id
        assert restored.token_len == original.token_len
        assert restored.mode == original.mode
        assert len(restored.instances) == 2
        assert restored.instances[0].node_id == "fn:a.py::helper"
        assert restored.instances[1].file == "b.py"
        assert restored.family_id is None


# ---------------------------------------------------------------------------
# RepoGraphBlob.clones field
# ---------------------------------------------------------------------------


class TestRepoGraphBlobClonesField:
    """clones field on RepoGraphBlob must default to [] and be backward-compat."""

    def test_clones_defaults_to_empty_list(self):
        blob = RepoGraphBlob(**_minimal_repo_graph_blob())
        assert blob.clones == []

    def test_old_blob_without_clones_key_validates_to_empty(self):
        """Backward-compat: a persisted dict without the clones key must
        deserialize to [] rather than raising a validation error."""
        data = _minimal_repo_graph_blob()
        data.pop("clones", None)
        blob = RepoGraphBlob.model_validate(data)
        assert blob.clones == []

    def test_clones_populated_survives_round_trip(self):
        group = CloneGroup(
            id="cg-rt2",
            token_len=50,
            mode="strict",
            instances=[
                CloneInstance(node_id="fn:a.py::helper", file="a.py", line_start=10, line_end=30),
                CloneInstance(node_id="fn:b.py::helper", file="b.py", line_start=5, line_end=25),
            ],
        )
        blob = RepoGraphBlob(**_minimal_repo_graph_blob(clones=[group]))
        dumped = blob.model_dump()
        restored = RepoGraphBlob.model_validate(dumped)
        assert len(restored.clones) == 1
        assert restored.clones[0].id == "cg-rt2"
        assert restored.clones[0].mode == "strict"

    def test_clones_field_present_in_model_dump(self):
        blob = RepoGraphBlob(**_minimal_repo_graph_blob())
        dumped = blob.model_dump()
        assert "clones" in dumped
        assert dumped["clones"] == []
