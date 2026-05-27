"""Size heuristic for gap-fix architect items.

Task 28 (2026-05-27) shipped a single backlog item titled
"Wire CounterfactualSession + REST endpoints + WebSocket sibling streaming"
that crammed three subsystems into one coder turn. The coder ground for
30+ minutes reading 1.94M tokens trying to implement it. The original
``ARCHITECT_INITIAL_SYSTEM`` prompt enforces a no-defer + split-or-spawn-
sub-architects rule, but the gap-fix path runs under a different
(checkpoint) system prompt and that rule never reached it.

These tests pin the heuristic + soft-warning behaviour:

- ``_OVERSIZED_TITLE_CONNECTIVES`` matches " + ", " and ", " with ".
- More than ``_MAX_CONNECTIVES_IN_TITLE`` (1) connectives in the title
  flags the item.
- A description naming more than ``_MAX_FILE_PATHS_IN_DESCRIPTION`` (4)
  file paths flags the item.
- The validator is purely warn-and-log — it does NOT block dispatch.
  Surfaced via ``decision["size_warnings"]`` so the UI can flag the
  rows; downstream re-dispatch is the LLM's job on the next gap-fix
  round.
"""

from __future__ import annotations


def test_validate_clean_item_returns_no_warnings():
    from agent.lifecycle.trio.gap_fix import validate_backlog_items

    items = [
        {
            "id": "G1",
            "title": "Build CounterfactualSession model",
            "description": "Add src/counterfactual/session.py with a Pydantic model.",
        }
    ]
    assert validate_backlog_items(items) == []


def test_validate_flags_multi_subsystem_title():
    """The task-28 G3 case: title stitches 3 subsystems with '+'."""
    from agent.lifecycle.trio.gap_fix import validate_backlog_items

    items = [
        {
            "id": "G3",
            "title": "Wire CounterfactualSession + REST endpoints + WebSocket streaming",
            "description": "Add the session and routes.",
        }
    ]
    warnings = validate_backlog_items(items)
    assert len(warnings) == 1
    assert warnings[0]["id"] == "G3"
    assert "stitches" in warnings[0]["reason"]
    assert "subsystems" in warnings[0]["reason"]


def test_validate_flags_description_with_too_many_file_paths():
    from agent.lifecycle.trio.gap_fix import validate_backlog_items

    items = [
        {
            "id": "G4",
            "title": "Counterfactuals tab",
            "description": (
                "Touch src/components/Tabs.tsx, src/views/Counterfactuals.tsx, "
                "src/hooks/useCounterfactuals.ts, src/api/counterfactuals.py, "
                "src/models/counterfactual.py, and tests/test_counterfactuals.py."
            ),
        }
    ]
    warnings = validate_backlog_items(items)
    assert len(warnings) == 1
    assert "file paths" in warnings[0]["reason"]


def test_validate_allows_single_connective():
    """A single 'and' in a title is fine — only >1 connectives flag it."""
    from agent.lifecycle.trio.gap_fix import validate_backlog_items

    items = [
        {
            "id": "G2",
            "title": "Build models and add migration",
            "description": "Add src/models/foo.py and migrations/0001_foo.py.",
        }
    ]
    assert validate_backlog_items(items) == []


def test_validate_skips_idless_or_titleless_items_gracefully():
    """Validator must not crash on malformed items — they're caught
    elsewhere; this is a heuristic, not a schema."""
    from agent.lifecycle.trio.gap_fix import validate_backlog_items

    items = [
        {},
        {"id": "G1"},
        {"title": "no id"},
    ]
    assert validate_backlog_items(items) == []


def test_validate_empty_list_returns_empty():
    from agent.lifecycle.trio.gap_fix import validate_backlog_items

    assert validate_backlog_items([]) == []
    assert validate_backlog_items(None) == []  # type: ignore[arg-type]
