"""Per-org workspace subdirectories."""

from __future__ import annotations

import os

from agent import workspace as ws


def test_per_org_workspace_path_when_org_id_set(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ws, "WORKSPACES_DIR", str(tmp_path))
    expected = os.path.join(str(tmp_path), "42", "task-7")
    actual = ws._workspace_path(task_id=7, organization_id=42)
    assert actual == expected


def test_legacy_workspace_path_when_org_id_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ws, "WORKSPACES_DIR", str(tmp_path))
    expected = os.path.join(str(tmp_path), "task-7")
    actual = ws._workspace_path(task_id=7, organization_id=None)
    assert actual == expected


def test_cleanup_workspace_only_removes_per_org_subtree(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ws, "WORKSPACES_DIR", str(tmp_path))
    a = os.path.join(str(tmp_path), "1", "task-7")
    b = os.path.join(str(tmp_path), "2", "task-7")
    os.makedirs(a)
    os.makedirs(b)

    ws.cleanup_workspace(task_id=7, organization_id=1)

    assert not os.path.exists(a)
    assert os.path.exists(b)  # other org untouched


def test_cleanup_workspace_legacy_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ws, "WORKSPACES_DIR", str(tmp_path))
    legacy = os.path.join(str(tmp_path), "task-7")
    os.makedirs(legacy)

    ws.cleanup_workspace(task_id=7)  # No org_id — legacy path

    assert not os.path.exists(legacy)
