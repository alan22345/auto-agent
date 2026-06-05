"""Tests for the DeadCodeFinding schema and RepoGraphBlob.dead_code field.

TDD: written before the schema exists to drive the additions in
shared/types.py (Task A of §4 dead-code feature, ADR-016 phase 10).
"""

from datetime import UTC, datetime

import pytest

from shared.types import (
    DeadCodeFinding,
    RepoGraphBlob,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_repo_graph_blob(**overrides) -> dict:
    """Return the minimum dict needed to construct a RepoGraphBlob."""
    base = {
        "commit_sha": "abc123",
        "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "analyser_version": "phase10-deadcode-0.10.0",
        "areas": [],
        "nodes": [],
        "edges": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# DeadCodeFinding construction and round-trip
# ---------------------------------------------------------------------------


class TestDeadCodeFindingConstruction:
    """DeadCodeFinding must construct from valid input and round-trip cleanly."""

    def test_basic_construction_unused_export(self):
        finding = DeadCodeFinding(
            kind="unused_export",
            target="a.py::unused_helper",
            file="a.py",
            reason="Symbol is never referenced outside its defining module.",
        )
        assert finding.kind == "unused_export"
        assert finding.target == "a.py::unused_helper"
        assert finding.file == "a.py"
        assert finding.reason == "Symbol is never referenced outside its defining module."

    def test_basic_construction_unused_file(self):
        finding = DeadCodeFinding(
            kind="unused_file",
            target="file:api/legacy.py",
            file="api/legacy.py",
            reason="No imports or edges point to this file.",
        )
        assert finding.kind == "unused_file"
        assert finding.target == "file:api/legacy.py"

    def test_unused_dependency_kind_accepted(self):
        finding = DeadCodeFinding(
            kind="unused_dependency",
            target="requests",
            file=None,
            reason="Package declared in requirements but never imported.",
        )
        assert finding.kind == "unused_dependency"

    def test_undeclared_dependency_kind_accepted(self):
        finding = DeadCodeFinding(
            kind="undeclared_dependency",
            target="boto3",
            file=None,
            reason="Imported but not declared in requirements.",
        )
        assert finding.kind == "undeclared_dependency"

    def test_file_field_is_optional(self):
        finding = DeadCodeFinding(
            kind="unused_export",
            target="a.py::f",
            reason="Never called.",
        )
        assert finding.file is None

    def test_invalid_kind_raises_validation_error(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            DeadCodeFinding(
                kind="bogus",
                target="a.py::f",
                reason="Should fail.",
            )

    def test_round_trip_model_dump_model_validate(self):
        original = DeadCodeFinding(
            kind="unused_export",
            target="a.py::f",
            file="a.py",
            reason="Never called from outside the module.",
        )
        dumped = original.model_dump()
        restored = DeadCodeFinding.model_validate(dumped)
        assert restored.kind == original.kind
        assert restored.target == original.target
        assert restored.file == original.file
        assert restored.reason == original.reason


# ---------------------------------------------------------------------------
# RepoGraphBlob.dead_code field
# ---------------------------------------------------------------------------


class TestRepoGraphBlobDeadCodeField:
    """dead_code field on RepoGraphBlob must default to [] and be backward-compat."""

    def test_dead_code_defaults_to_empty_list(self):
        blob = RepoGraphBlob(**_minimal_repo_graph_blob())
        assert blob.dead_code == []

    def test_old_blob_without_dead_code_key_validates_to_empty(self):
        """Backward-compat: a persisted dict without the dead_code key must
        deserialize to [] rather than raising a validation error."""
        data = _minimal_repo_graph_blob()
        data.pop("dead_code", None)
        blob = RepoGraphBlob.model_validate(data)
        assert blob.dead_code == []

    def test_dead_code_populated_survives_round_trip(self):
        finding = DeadCodeFinding(
            kind="unused_export",
            target="a.py::f",
            file="a.py",
            reason="Never called.",
        )
        blob = RepoGraphBlob(**_minimal_repo_graph_blob(dead_code=[finding]))
        dumped = blob.model_dump()
        restored = RepoGraphBlob.model_validate(dumped)
        assert len(restored.dead_code) == 1
        assert restored.dead_code[0].kind == "unused_export"
        assert restored.dead_code[0].target == "a.py::f"

    def test_dead_code_field_present_in_model_dump(self):
        blob = RepoGraphBlob(**_minimal_repo_graph_blob())
        dumped = blob.model_dump()
        assert "dead_code" in dumped
        assert dumped["dead_code"] == []

    def test_multiple_findings_in_blob(self):
        findings = [
            DeadCodeFinding(kind="unused_export", target="a.py::helper", file="a.py", reason="x"),
            DeadCodeFinding(kind="unused_file", target="file:old.py", file="old.py", reason="y"),
        ]
        blob = RepoGraphBlob(**_minimal_repo_graph_blob(dead_code=findings))
        dumped = blob.model_dump()
        restored = RepoGraphBlob.model_validate(dumped)
        assert len(restored.dead_code) == 2
        assert restored.dead_code[0].kind == "unused_export"
        assert restored.dead_code[1].kind == "unused_file"
