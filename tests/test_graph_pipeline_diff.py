"""Tests for agent/graph_analyzer/diff.py — git diff into a ChangedFilesPlan."""

from agent.graph_analyzer.diff import (
    ChangedFilesPlan,
    _parse_git_name_status,
)


def test_added_modified_deleted() -> None:
    raw = b"A\x00new.ts\x00M\x00mod.ts\x00D\x00gone.ts\x00"
    plan = _parse_git_name_status(raw)
    assert plan == ChangedFilesPlan(
        added=["new.ts"],
        modified=["mod.ts"],
        deleted=["gone.ts"],
        renamed_pure=[],
        renamed_modified=[],
    )


def test_pure_rename() -> None:
    raw = b"R100\x00old.ts\x00new.ts\x00"
    plan = _parse_git_name_status(raw)
    assert plan.renamed_pure == [("old.ts", "new.ts")]
    assert plan.modified == []
    assert plan.added == []


def test_rename_with_modify() -> None:
    raw = b"R75\x00old.ts\x00new.ts\x00"
    plan = _parse_git_name_status(raw)
    assert plan.renamed_modified == [("old.ts", "new.ts")]
    assert plan.renamed_pure == []


def test_type_change_treated_as_modify() -> None:
    raw = b"T\x00convert.ts\x00"
    plan = _parse_git_name_status(raw)
    assert plan.modified == ["convert.ts"]


def test_paths_with_spaces() -> None:
    raw = b"M\x00path with space.ts\x00A\x00normal.ts\x00"
    plan = _parse_git_name_status(raw)
    assert "path with space.ts" in plan.modified
    assert "normal.ts" in plan.added


def test_empty_diff_is_empty_plan() -> None:
    plan = _parse_git_name_status(b"")
    assert plan == ChangedFilesPlan(
        added=[], modified=[], deleted=[], renamed_pure=[], renamed_modified=[]
    )
