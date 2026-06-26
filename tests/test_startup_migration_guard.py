"""Startup must refuse to serve against a stale schema.

The old behaviour logged a failed migration and booted anyway — so the app ran
against alembic 047, where it couldn't read the code graph and couldn't persist
tasks (writes hit columns that don't exist), silently dropping data. The guard
now VERIFIES the DB reached head and raises otherwise, turning silent data loss
into a loud, safe crash-loop. These tests pin that contract without a database.
"""

from __future__ import annotations

from pathlib import Path

import alembic.command
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

import run


def _expected_heads() -> set[str]:
    cfg = Config(str(Path(run.__file__).parent / "alembic.ini"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def test_refuses_to_start_when_not_at_head(monkeypatch) -> None:
    monkeypatch.setattr(run, "_MIGRATION_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(run.time, "sleep", lambda *_a, **_k: None)
    # The migration "runs" but the DB never reaches head (e.g. it rolled back).
    monkeypatch.setattr(alembic.command, "upgrade", lambda *_a, **_k: None)
    monkeypatch.setattr(run, "_db_current_heads", lambda: {"047"})

    with pytest.raises(RuntimeError, match="stale schema"):
        run._ensure_schema_at_head_sync()


def test_raises_when_migration_itself_throws(monkeypatch) -> None:
    monkeypatch.setattr(run, "_MIGRATION_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(run.time, "sleep", lambda *_a, **_k: None)

    def _boom(*_a, **_k):
        raise RuntimeError("DDL lock timeout")

    monkeypatch.setattr(alembic.command, "upgrade", _boom)
    monkeypatch.setattr(run, "_db_current_heads", lambda: {"047"})

    with pytest.raises(RuntimeError, match="not at alembic head"):
        run._ensure_schema_at_head_sync()


def test_starts_when_schema_reaches_head(monkeypatch) -> None:
    monkeypatch.setattr(alembic.command, "upgrade", lambda *_a, **_k: None)
    monkeypatch.setattr(run, "_db_current_heads", _expected_heads)
    # Returns normally — no raise — when the DB is verified at head.
    run._ensure_schema_at_head_sync()


def test_retries_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(run, "_MIGRATION_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(run.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(alembic.command, "upgrade", lambda *_a, **_k: None)

    heads = _expected_heads()
    calls = {"n": 0}

    def _heads_after_two_failures() -> set[str]:
        calls["n"] += 1
        return heads if calls["n"] >= 2 else {"047"}

    monkeypatch.setattr(run, "_db_current_heads", _heads_after_two_failures)
    run._ensure_schema_at_head_sync()  # 1st attempt behind, 2nd at head → no raise
    assert calls["n"] == 2
