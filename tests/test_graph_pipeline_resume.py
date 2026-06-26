"""Tests for the diff-plan application + smart-cascade logic.

Edges use FLAT string ``source``/``target`` node ids (see
``shared.types.Edge`` — ``source: str``, ``target: str``), NOT nested
``{"id", "file"}`` dicts. An earlier version of these tests used the nested
shape, which let ``apply_plan`` pass in tests while crashing in prod with
``string indices must be integers, not 'str'`` on every refresh-after-commit
(the ``resume_diff`` path). ``test_apply_plan_handles_real_edge_shape`` locks
the real contract.
"""

from agent.graph_analyzer.diff import ChangedFilesPlan, apply_plan


def _blob():
    return {
        "nodes": [
            {"id": "a.ts::foo", "file": "a.ts"},
            {"id": "a.ts::bar", "file": "a.ts"},
            {"id": "b.ts::caller", "file": "b.ts"},
        ],
        "edges": [
            {"source": "b.ts::caller", "target": "a.ts::foo"},
            {"source": "a.ts::bar", "target": "a.ts::foo"},
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
    # edge endpoint ids are rewritten too
    assert any(e["target"] == "moved/a.ts::foo" for e in blob["edges"])


def test_apply_plan_handles_real_edge_shape():
    """Regression: edges carry the real serialized ``shared.types.Edge`` shape
    (flat string source/target + evidence/kind/source_kind). apply_plan must
    not raise ``string indices must be integers`` on it."""
    blob = {
        "nodes": [
            {"id": "a.ts::foo", "file": "a.ts", "kind": "function", "area": "x"},
            {"id": "b.ts::caller", "file": "b.ts", "kind": "function", "area": "x"},
        ],
        "edges": [
            {
                "source": "b.ts::caller",
                "target": "a.ts::foo",
                "kind": "calls",
                "evidence": {"file": "b.ts", "line": 3, "snippet": "foo()"},
                "source_kind": "ast",
            },
        ],
    }
    processed = {"a.ts": {}, "b.ts": {}}
    cascade = apply_plan(
        blob,
        processed,
        ChangedFilesPlan(modified=["a.ts"]),
        re_walk=lambda p: {"nodes_in_path": []},
    )
    # caller in b.ts referenced a now-lost target in a.ts → must cascade
    assert "b.ts" in cascade
    # the a.ts node/edge were pruned for re-walk
    assert all(n["file"] != "a.ts" for n in blob["nodes"])
