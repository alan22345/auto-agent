"""Unit tests for orchestrator.scoping.scoped()."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from orchestrator.scoping import scoped
from shared.models import (
    FreeformConfig,
    Repo,
    ScheduledTask,
    SearchMessage,
    Suggestion,
    Task,
    TaskHistory,
    TaskMessage,
    TaskOutcome,
    UserSecret,
)


def _sql(q) -> str:
    return str(q.compile(compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# Direct-scoped models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,table",
    [
        (Repo, "repos"),
        (Task, "tasks"),
        (Suggestion, "suggestions"),
        (FreeformConfig, "freeform_configs"),
        (ScheduledTask, "scheduled_tasks"),
        (UserSecret, "user_secrets"),
    ],
)
def test_scoped_direct_adds_org_filter(model, table):
    q = scoped(select(model), model, org_id=42)
    sql = _sql(q)
    assert f"{table}.organization_id = 42" in sql


# ---------------------------------------------------------------------------
# Transitively-scoped models
# ---------------------------------------------------------------------------


def test_scoped_task_history_joins_through_task():
    q = scoped(select(TaskHistory), TaskHistory, org_id=42)
    sql = _sql(q)
    assert "task_history" in sql
    assert "tasks.organization_id = 42" in sql
    assert "join tasks" in sql.lower()


def test_scoped_task_message_joins_through_task():
    q = scoped(select(TaskMessage), TaskMessage, org_id=42)
    sql = _sql(q)
    assert "tasks.organization_id = 42" in sql
    assert "join tasks" in sql.lower()


def test_scoped_task_outcome_joins_through_task():
    q = scoped(select(TaskOutcome), TaskOutcome, org_id=42)
    sql = _sql(q)
    assert "tasks.organization_id = 42" in sql


def test_scoped_search_message_joins_through_session():
    q = scoped(select(SearchMessage), SearchMessage, org_id=42)
    sql = _sql(q)
    assert "search_sessions.organization_id = 42" in sql
    assert "join search_sessions" in sql.lower()


# ---------------------------------------------------------------------------
# Unregistered models fail loud
# ---------------------------------------------------------------------------


def test_scoped_rejects_unknown_model():
    class Bogus:
        pass

    with pytest.raises(KeyError, match="not registered"):
        scoped(select(Repo), Bogus, org_id=42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Composition with existing predicates
# ---------------------------------------------------------------------------


def test_scoped_composes_with_existing_where():
    q = scoped(select(Task).where(Task.id == 5), Task, org_id=42)
    sql = _sql(q)
    assert "tasks.id = 5" in sql
    assert "tasks.organization_id = 42" in sql
