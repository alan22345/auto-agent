"""Tests for the diff-plan application + smart-cascade logic."""

from agent.graph_analyzer.diff import ChangedFilesPlan, apply_plan


def _blob():
    return {
        "nodes": [
            {"id": "a.ts::foo", "file": "a.ts"},
            {"id": "a.ts::bar", "file": "a.ts"},
            {"id": "b.ts::caller", "file": "b.ts"},
        ],
        "edges": [
            {"source": {"id": "b.ts::caller", "file": "b.ts"},
             "target": {"id": "a.ts::foo", "file": "a.ts"}},
            {"source": {"id": "a.ts::bar", "file": "a.ts"},
             "target": {"id": "a.ts::foo", "file": "a.ts"}},
        ],
    }


def test_deleted_file_prunes_and_cascades():
    blob = _blob()
    processed = {
        "a.ts": {"sites_attempted": 2},
        "b.ts": {"sites_attempted": 1},
    }
    plan = ChangedFilesPlan(deleted=["a.ts"])
    cascade = apply_plan(blob, processed, plan)
    assert not any(n["file"] == "a.ts" for n in blob["nodes"])
    assert blob["edges"] == []
    assert "a.ts" not in processed
    assert "b.ts" in cascade
    assert "b.ts" not in processed


def test_modified_with_lost_target_cascades():
    blob = _blob()
    processed = {"a.ts": {"sites_attempted": 2}, "b.ts": {"sites_attempted": 1}}
    plan = ChangedFilesPlan(modified=["a.ts"])
    def re_walk(path):
        return {"nodes_in_path": [{"id": "a.ts::bar", "file": "a.ts"}]}
    cascade = apply_plan(blob, processed, plan, re_walk=re_walk)
    assert "b.ts" in cascade
    assert "a.ts" not in processed
    assert "b.ts" not in processed


def test_modified_with_target_preserved_no_cascade():
    blob = _blob()
    processed = {"a.ts": {"sites_attempted": 2}, "b.ts": {"sites_attempted": 1}}
    plan = ChangedFilesPlan(modified=["a.ts"])
    def re_walk(path):
        return {
            "nodes_in_path": [
                {"id": "a.ts::foo", "file": "a.ts"},
                {"id": "a.ts::bar", "file": "a.ts"},
            ]
        }
    cascade = apply_plan(blob, processed, plan, re_walk=re_walk)
    assert cascade == set()
    assert "a.ts" not in processed
    assert "b.ts" in processed


def test_pure_rename_rewrites_paths_no_cascade():
    blob = _blob()
    processed = {"a.ts": {"sites_attempted": 2}}
    plan = ChangedFilesPlan(renamed_pure=[("a.ts", "moved/a.ts")])
    cascade = apply_plan(blob, processed, plan)
    assert cascade == set()
    assert "a.ts" not in processed
    assert "moved/a.ts" in processed
    assert all(
        n["file"] == "moved/a.ts"
        for n in blob["nodes"]
        if "foo" in n["id"] or "bar" in n["id"]
    )
