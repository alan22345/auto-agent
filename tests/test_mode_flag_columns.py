"""ADR-015 §7 — per-repo mode flag + per-task mode override columns.

The schema gains two columns:

  * ``Repo.mode`` — ``"freeform" | "human_in_loop"``, default ``"human_in_loop"``.
  * ``Task.mode_override`` — same domain, nullable; ``None`` means inherit
    from the repo.

These tests check the ORM-side declaration. The migration that creates
the columns in Postgres lives under ``migrations/versions/``; the
``test_*_migration.py`` family covers schema landing in a real DB.
"""

from __future__ import annotations

from shared.models import Repo, Task


def test_repo_has_mode_column() -> None:
    assert "mode" in Repo.__table__.columns


def test_repo_mode_default_is_human_in_loop() -> None:
    col = Repo.__table__.columns["mode"]
    default = col.default.arg if col.default is not None else None
    assert default == "human_in_loop"


def test_task_has_mode_override_column() -> None:
    assert "mode_override" in Task.__table__.columns


def test_task_mode_override_is_nullable() -> None:
    """``None`` means "inherit from the repo" — must be nullable."""

    col = Task.__table__.columns["mode_override"]
    assert col.nullable is True
