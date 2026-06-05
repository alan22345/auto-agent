"""Tests for the Hotspot schema and RepoGraphBlob.hotspots field.

TDD: written before the schema exists to drive the additions in
shared/types.py (Task A of §5 churn-hotspot feature, ADR-016 phase 12).
"""

from datetime import UTC, datetime

import pydantic
import pytest

from shared.types import (
    Hotspot,
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
        "analyser_version": "phase12-hotspots-0.12.0",
        "areas": [],
        "nodes": [],
        "edges": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Hotspot construction and round-trip
# ---------------------------------------------------------------------------


class TestHotspotConstruction:
    """Hotspot must construct from valid input and round-trip cleanly."""

    def test_basic_construction(self):
        h = Hotspot(file="a.py", churn=1.5, complexity_density=0.8, score=42.0, trend="stable")
        assert h.file == "a.py"
        assert h.churn == 1.5
        assert h.complexity_density == 0.8
        assert h.score == 42.0
        assert h.trend == "stable"

    def test_round_trip_model_dump_model_validate(self):
        original = Hotspot(
            file="a.py", churn=1.5, complexity_density=0.8, score=42.0, trend="stable"
        )
        dumped = original.model_dump()
        restored = Hotspot.model_validate(dumped)
        assert restored.file == original.file
        assert restored.churn == original.churn
        assert restored.complexity_density == original.complexity_density
        assert restored.score == original.score
        assert restored.trend == original.trend

    def test_all_trend_values_accepted(self):
        for trend in ("accelerating", "stable", "cooling"):
            h = Hotspot(file="b.py", churn=2.0, complexity_density=0.5, score=30.0, trend=trend)
            assert h.trend == trend

    def test_invalid_trend_raises_validation_error(self):
        with pytest.raises(pydantic.ValidationError):
            Hotspot(file="c.py", churn=1.0, complexity_density=0.4, score=20.0, trend="bogus")


# ---------------------------------------------------------------------------
# RepoGraphBlob.hotspots field
# ---------------------------------------------------------------------------


class TestRepoGraphBlobHotspotsField:
    """hotspots field on RepoGraphBlob must default to [] and be backward-compat."""

    def test_hotspots_defaults_to_empty_list(self):
        blob = RepoGraphBlob(**_minimal_repo_graph_blob())
        assert blob.hotspots == []

    def test_old_blob_without_hotspots_key_validates_to_empty(self):
        """Backward-compat: a persisted dict without the hotspots key must
        deserialize to [] rather than raising a validation error."""
        data = _minimal_repo_graph_blob()
        data.pop("hotspots", None)
        blob = RepoGraphBlob.model_validate(data)
        assert blob.hotspots == []

    def test_hotspots_populated_survives_round_trip(self):
        h = Hotspot(file="a.py", churn=1.5, complexity_density=0.8, score=42.0, trend="stable")
        blob = RepoGraphBlob(**_minimal_repo_graph_blob(hotspots=[h]))
        dumped = blob.model_dump()
        restored = RepoGraphBlob.model_validate(dumped)
        assert len(restored.hotspots) == 1
        assert restored.hotspots[0].file == "a.py"
        assert restored.hotspots[0].score == 42.0

    def test_hotspots_field_present_in_model_dump(self):
        blob = RepoGraphBlob(**_minimal_repo_graph_blob())
        dumped = blob.model_dump()
        assert "hotspots" in dumped
        assert dumped["hotspots"] == []
