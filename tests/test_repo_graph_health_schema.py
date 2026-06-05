"""Schema tests for FileHealth, RepoHealth, and RepoGraphBlob health fields.

TDD: validates the schema additions in shared/types.py for ADR-016 §6 Phase 13.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pydantic
import pytest

from shared.types import (
    FileHealth,
    RepoGraphBlob,
    RepoHealth,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_blob(**overrides) -> dict:
    base = {
        "commit_sha": "abc123",
        "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "analyser_version": "phase13-health-0.13.0",
        "areas": [],
        "nodes": [],
        "edges": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# FileHealth construction and round-trip
# ---------------------------------------------------------------------------


class TestFileHealthConstruction:
    def test_basic_construction(self):
        fh = FileHealth(file="src/foo.py", maintainability_index=85.0, band="good")
        assert fh.file == "src/foo.py"
        assert fh.maintainability_index == 85.0
        assert fh.band == "good"
        assert fh.crap is None

    def test_moderate_band(self):
        fh = FileHealth(file="src/bar.py", maintainability_index=55.0, band="moderate")
        assert fh.band == "moderate"

    def test_poor_band(self):
        fh = FileHealth(file="src/baz.py", maintainability_index=20.0, band="poor")
        assert fh.band == "poor"

    def test_crap_default_is_none(self):
        fh = FileHealth(file="src/foo.py", maintainability_index=70.0, band="good")
        assert fh.crap is None

    def test_round_trip(self):
        original = FileHealth(file="src/foo.py", maintainability_index=72.5, band="good", crap=None)
        restored = FileHealth.model_validate(original.model_dump())
        assert restored.file == original.file
        assert restored.maintainability_index == original.maintainability_index
        assert restored.band == original.band
        assert restored.crap is None

    def test_invalid_band_raises(self):
        with pytest.raises(pydantic.ValidationError):
            FileHealth(file="src/foo.py", maintainability_index=85.0, band="excellent")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RepoHealth construction and round-trip
# ---------------------------------------------------------------------------


class TestRepoHealthConstruction:
    def test_basic_construction(self):
        rh = RepoHealth(
            score=78.5,
            clone_count=3,
            cycle_count=1,
            dead_count=5,
            hotspot_count=2,
        )
        assert rh.score == 78.5
        assert rh.clone_count == 3
        assert rh.cycle_count == 1
        assert rh.dead_count == 5
        assert rh.hotspot_count == 2

    def test_round_trip(self):
        original = RepoHealth(
            score=90.0, clone_count=0, cycle_count=0, dead_count=0, hotspot_count=0
        )
        restored = RepoHealth.model_validate(original.model_dump())
        assert restored.score == original.score
        assert restored.clone_count == 0


# ---------------------------------------------------------------------------
# RepoGraphBlob field defaults and backward compat
# ---------------------------------------------------------------------------


class TestRepoGraphBlobHealthFields:
    def test_file_health_defaults_to_empty_list(self):
        blob = RepoGraphBlob.model_validate(_minimal_blob())
        assert blob.file_health == []

    def test_health_defaults_to_none(self):
        blob = RepoGraphBlob.model_validate(_minimal_blob())
        assert blob.health is None

    def test_backward_compat_dict_missing_both_keys(self):
        """A dict without file_health/health (pre-Phase-13 blob) must still validate."""
        data = _minimal_blob()
        # Explicitly ensure keys are absent
        data.pop("file_health", None)
        data.pop("health", None)
        blob = RepoGraphBlob.model_validate(data)
        assert blob.file_health == []
        assert blob.health is None

    def test_file_health_populated(self):
        fh_data = [{"file": "a.py", "maintainability_index": 80.0, "band": "good"}]
        blob = RepoGraphBlob.model_validate(_minimal_blob(file_health=fh_data))
        assert len(blob.file_health) == 1
        assert blob.file_health[0].file == "a.py"

    def test_health_populated(self):
        health_data = {
            "score": 75.0,
            "clone_count": 1,
            "cycle_count": 0,
            "dead_count": 2,
            "hotspot_count": 0,
        }
        blob = RepoGraphBlob.model_validate(_minimal_blob(health=health_data))
        assert blob.health is not None
        assert blob.health.score == 75.0
