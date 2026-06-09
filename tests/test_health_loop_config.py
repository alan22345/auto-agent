"""Phase 5a — HealthLoopConfig model + config service."""

from __future__ import annotations


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
