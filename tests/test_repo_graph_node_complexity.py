"""Tests for Node complexity fields (cyclomatic, cognitive, loc).

TDD: written before the fields exist to drive the schema change in
shared/types.py (Task 1 of the graph-complexity-scoring feature).
"""

import pytest

from shared.types import Node


def _minimal_node(**overrides) -> dict:
    """Return the minimum dict needed to construct a Node."""
    base = {
        "id": "test::fn",
        "kind": "function",
        "label": "my_func",
        "area": "core",
    }
    base.update(overrides)
    return base


class TestNodeComplexityFieldsAcceptValues:
    """Node must accept integer values for the three new fields."""

    def test_cyclomatic_stored(self):
        node = Node(**_minimal_node(cyclomatic=5))
        assert node.cyclomatic == 5

    def test_cognitive_stored(self):
        node = Node(**_minimal_node(cognitive=12))
        assert node.cognitive == 12

    def test_loc_stored(self):
        node = Node(**_minimal_node(loc=42))
        assert node.loc == 42

    def test_all_three_stored(self):
        node = Node(**_minimal_node(cyclomatic=3, cognitive=7, loc=20))
        assert node.cyclomatic == 3
        assert node.cognitive == 7
        assert node.loc == 20


class TestNodeComplexityFieldsRoundTrip:
    """Fields must survive .model_dump() -> Node.model_validate() round-trip."""

    def test_round_trip_with_values(self):
        original = Node(**_minimal_node(cyclomatic=3, cognitive=7, loc=20))
        dumped = original.model_dump()
        restored = Node.model_validate(dumped)
        assert restored.cyclomatic == 3
        assert restored.cognitive == 7
        assert restored.loc == 20

    def test_round_trip_with_none(self):
        original = Node(**_minimal_node())
        dumped = original.model_dump()
        restored = Node.model_validate(dumped)
        assert restored.cyclomatic is None
        assert restored.cognitive is None
        assert restored.loc is None


class TestNodeComplexityFieldsBackwardCompat:
    """Old blobs (dicts without the new keys) must still deserialise."""

    def test_construct_without_fields_defaults_none(self):
        """Constructing a Node without the new fields gives None defaults."""
        node = Node(**_minimal_node())
        assert node.cyclomatic is None
        assert node.cognitive is None
        assert node.loc is None

    def test_model_validate_missing_keys_defaults_none(self):
        """A persisted dict that lacks the keys validates to None — not KeyError."""
        blob = {
            "id": "old::fn",
            "kind": "function",
            "label": "old_func",
            "area": "legacy",
        }
        node = Node.model_validate(blob)
        assert node.cyclomatic is None
        assert node.cognitive is None
        assert node.loc is None

    def test_model_validate_explicit_none_passes(self):
        """Explicit None values from serialised blobs are also fine."""
        blob = _minimal_node(cyclomatic=None, cognitive=None, loc=None)
        node = Node.model_validate(blob)
        assert node.cyclomatic is None
        assert node.cognitive is None
        assert node.loc is None


class TestNodeComplexityFieldsNonNegative:
    """Negative values must be rejected — complexity metrics cannot be negative."""

    def test_negative_cyclomatic_raises(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            Node(**_minimal_node(cyclomatic=-1))

    def test_negative_cognitive_raises(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            Node(**_minimal_node(cognitive=-1))

    def test_negative_loc_raises(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            Node(**_minimal_node(loc=-1))
