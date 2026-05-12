"""Phase 4 ORM models — column / FK introspection."""

from __future__ import annotations

from shared.models import Organization, Plan, TaskStatus, UsageEvent


def test_plan_columns() -> None:
    cols = {c.name for c in Plan.__table__.columns}
    assert cols == {
        "id", "name", "max_concurrent_tasks", "max_tasks_per_day",
        "max_input_tokens_per_day", "max_output_tokens_per_day",
        "max_members", "monthly_price_cents",
    }


def test_organization_has_plan_fk() -> None:
    org_cols = {c.name for c in Organization.__table__.columns}
    assert "plan_id" in org_cols
    assert hasattr(Organization, "plan")


def test_usage_event_columns() -> None:
    cols = {c.name for c in UsageEvent.__table__.columns}
    assert cols == {
        "id", "org_id", "task_id", "kind", "model",
        "input_tokens", "output_tokens", "cost_cents", "occurred_at",
    }


def test_blocked_on_quota_enum_value_exists() -> None:
    assert TaskStatus.BLOCKED_ON_QUOTA.value == "blocked_on_quota"
