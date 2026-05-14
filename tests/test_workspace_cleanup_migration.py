"""Migration 042 workspace-cleanup tests — ADR-015 §2 Phase 7.6.

The migration ``rm -rf``s per-task workspace dirs that DB-level TRUNCATE
leaves behind (migration 041 only wipes rows, not disk). Verifies:

- Per-task globs match the right dirs (``task-*``, ``<org>/task-*``).
- System workspaces (``arch-*``, ``po-*``, ``harness-*``, ``summary-*``)
  are preserved.
- The migration is idempotent on empty directories.
"""

from __future__ import annotations

import os
import subprocess
import tempfile


def _shell(pattern: str, rooted_at: str) -> None:
    """Run the same shell loop migration 042 runs, but rooted at a tmp dir.

    Uses ``check=False`` and ``set +e`` to mirror migration 042's tolerance
    of unexpanded globs (when no dirs match, ``test -d "$d"`` on the literal
    pattern fails — that's expected and must not propagate).
    """

    cmd = f'set +e; for d in {rooted_at}{pattern}; do test -d "$d" && rm -rf "$d"; done; true'
    subprocess.run(cmd, shell=True, check=False)


def test_legacy_task_dirs_deleted_per_org_dirs_deleted_system_dirs_preserved():
    with tempfile.TemporaryDirectory() as root:
        ws = os.path.join(root, "workspaces")
        os.makedirs(ws)
        # Legacy task workspaces (no org prefix).
        for name in ("task-100", "task-101", "task-999"):
            os.makedirs(os.path.join(ws, name))
        # Per-org task workspaces.
        org1 = os.path.join(ws, "1")
        os.makedirs(org1)
        for name in ("task-1", "task-7", "task-200"):
            os.makedirs(os.path.join(org1, name))
        # System workspaces — must SURVIVE.
        for name in (
            "arch-cardamon",
            "arch-iot-apartment-simulator",
            "po-auto-agent",
            "harness-alan22345-auto-agent",
            "summary-cardamon",
        ):
            os.makedirs(os.path.join(ws, name))

        _shell("/workspaces/task-*", root)
        _shell("/workspaces/[0-9]*/task-*", root)

        survivors = sorted(os.listdir(ws))
        # System workspaces preserved, plus the empty org1 dir.
        assert "task-100" not in survivors
        assert "task-101" not in survivors
        assert "task-999" not in survivors
        assert "arch-cardamon" in survivors
        assert "arch-iot-apartment-simulator" in survivors
        assert "po-auto-agent" in survivors
        assert "harness-alan22345-auto-agent" in survivors
        assert "summary-cardamon" in survivors
        # Org dirs survive (just emptied of task children).
        assert "1" in survivors
        assert os.listdir(org1) == []


def test_system_workspaces_named_task_inside_are_not_touched():
    """A defensive case — the patterns must not descend into ``arch-*`` or
    similar even if those dirs happen to contain a child named ``task-*``."""

    with tempfile.TemporaryDirectory() as root:
        ws = os.path.join(root, "workspaces")
        arch = os.path.join(ws, "arch-cardamon")
        os.makedirs(arch)
        # A child INSIDE the arch workspace happens to be called task-7.
        # This shouldn't match the patterns, which are scoped to exactly two
        # path segments under /workspaces.
        os.makedirs(os.path.join(arch, "task-7"))

        _shell("/workspaces/task-*", root)
        _shell("/workspaces/[0-9]*/task-*", root)

        # arch-cardamon/task-7 SHOULD survive because ``arch-cardamon`` is
        # not matched by either glob — it doesn't start with ``task-``.
        assert os.path.isdir(os.path.join(arch, "task-7"))


def test_idempotent_on_empty_workspaces():
    """Running the migration on an empty workspaces dir must not error."""

    with tempfile.TemporaryDirectory() as root:
        ws = os.path.join(root, "workspaces")
        os.makedirs(ws)
        _shell("/workspaces/task-*", root)
        _shell("/workspaces/[0-9]*/task-*", root)
        # No exception raised.
        assert os.path.isdir(ws)


def test_missing_workspaces_dir_is_not_fatal():
    """Fresh container with no /workspaces yet — must not crash."""

    with tempfile.TemporaryDirectory() as root:
        # No /workspaces directory created at all.
        _shell("/workspaces/task-*", root)
        _shell("/workspaces/[0-9]*/task-*", root)
        # No exception raised; the tmp dir is left empty.
        assert os.listdir(root) == []
