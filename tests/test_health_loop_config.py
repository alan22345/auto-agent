"""Phase 5a — HealthLoopConfig model + config service."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_health_loop_config_columns():
    from shared.models import HealthLoopConfig

    cols = {c.name for c in HealthLoopConfig.__table__.columns}
    assert cols == {
        "repo_id",
        "organization_id",
        "enabled",
        "cleanup_branch",
        "batch_size",
        "state",
        "suppressed_finding_hashes",
        "supervisor_task_id",
        "last_run_at",
        "created_at",
        "updated_at",
    }
    assert HealthLoopConfig.__tablename__ == "health_loop_configs"
    # repo_id is the PK (1:1 with repo).
    pk = {c.name for c in HealthLoopConfig.__table__.primary_key.columns}
    assert pk == {"repo_id"}


def _load_migration():
    path = Path("migrations/versions/055_health_loop_config.py")
    spec = importlib.util.spec_from_file_location("m055", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_055_chains_off_054_and_defines_up_down():
    m = _load_migration()
    assert m.revision == "055"
    assert m.down_revision == "054"
    assert callable(m.upgrade)
    assert callable(m.downgrade)


def test_dedup_append_adds_once_and_preserves_order():
    from agent.health_loop.config_service import _dedup_append

    assert _dedup_append(["a", "b"], "c") == ["a", "b", "c"]
    # Already present ⇒ unchanged (no duplicate).
    assert _dedup_append(["a", "b"], "a") == ["a", "b"]
    # Empty start.
    assert _dedup_append([], "x") == ["x"]
