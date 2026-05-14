"""Defensive shim for in-flight workspaces — ADR-015 §12 + §15.

Per §15, all in-flight tasks are dropped at deploy time, so this shim is
defensive only. It exists to keep the system robust to leftover artefacts
on disk after the deploy migration runs: if a workspace still has a
``.trio/`` directory (the old workspace name) and no ``.auto-agent/``,
rename it in place. If both exist, leave alone (a warning is enough — we
do not want to clobber whatever the new flow already wrote).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.workspace import migrate_trio_workspace

if TYPE_CHECKING:
    from pathlib import Path


def test_renames_trio_to_auto_agent_when_only_trio_exists(tmp_path: Path) -> None:
    trio = tmp_path / ".trio"
    trio.mkdir()
    (trio / "foo.json").write_text("{}")

    migrate_trio_workspace(str(tmp_path))

    assert not trio.exists()
    new_dir = tmp_path / ".auto-agent"
    assert new_dir.is_dir()
    assert (new_dir / "foo.json").read_text() == "{}"


def test_idempotent_when_neither_dir_exists(tmp_path: Path) -> None:
    # No-op should not raise.
    migrate_trio_workspace(str(tmp_path))
    assert not (tmp_path / ".trio").exists()
    assert not (tmp_path / ".auto-agent").exists()


def test_idempotent_when_only_auto_agent_exists(tmp_path: Path) -> None:
    target = tmp_path / ".auto-agent"
    target.mkdir()
    (target / "design.md").write_text("# design")

    migrate_trio_workspace(str(tmp_path))

    # No-op: the destination is untouched.
    assert target.is_dir()
    assert (target / "design.md").read_text() == "# design"


def test_leaves_both_in_place_when_both_exist(tmp_path: Path) -> None:
    trio = tmp_path / ".trio"
    trio.mkdir()
    (trio / "legacy.json").write_text("legacy")

    target = tmp_path / ".auto-agent"
    target.mkdir()
    (target / "new.json").write_text("new")

    migrate_trio_workspace(str(tmp_path))

    # Both untouched; this is the "log a warning, leave alone" branch.
    assert trio.is_dir()
    assert (trio / "legacy.json").read_text() == "legacy"
    assert target.is_dir()
    assert (target / "new.json").read_text() == "new"
