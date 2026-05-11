"""Query scoping helper for the multi-tenant model.

Every query that returns customer-visible rows must pass through
``scoped()`` so it filters by the requester's active organization.
Models fall into two buckets:

* **Direct-scoped** — has its own ``organization_id`` column. Filter
  is a simple ``WHERE model.organization_id = :org_id``.
* **Transitively-scoped** — lives under a direct-scoped parent. Filter
  joins the parent and adds a WHERE on the parent's ``organization_id``.

A model that isn't registered here raises ``KeyError``. Silent
fall-through is how multi-tenant data leaks happen, so failing loud
is the contract: if you find yourself wanting to call ``scoped`` on a
new model, add it to the registry below — don't bypass it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select

from shared.models import (
    FreeformConfig,
    Repo,
    ScheduledTask,
    SearchMessage,
    SearchSession,
    Suggestion,
    Task,
    TaskHistory,
    TaskMessage,
    TaskOutcome,
    UserSecret,
)

# Models with their own ``organization_id`` column.
_DIRECT_SCOPED: dict[type, Any] = {
    Repo: Repo.organization_id,
    Task: Task.organization_id,
    Suggestion: Suggestion.organization_id,
    FreeformConfig: FreeformConfig.organization_id,
    ScheduledTask: ScheduledTask.organization_id,
    SearchSession: SearchSession.organization_id,
    UserSecret: UserSecret.organization_id,
}


# Models scoped through a parent row.
# Maps child -> (parent_cls, child_fk_attr, parent_org_id_attr).
_TRANSITIVE_SCOPED: dict[type, tuple[type, Any, Any]] = {
    TaskHistory: (Task, TaskHistory.task_id, Task.organization_id),
    TaskMessage: (Task, TaskMessage.task_id, Task.organization_id),
    TaskOutcome: (Task, TaskOutcome.task_id, Task.organization_id),
    SearchMessage: (
        SearchSession,
        SearchMessage.session_id,
        SearchSession.organization_id,
    ),
}


def scoped(query: Select, model: type, *, org_id: int) -> Select:
    """Append the tenant filter to ``query``.

    Raises ``KeyError`` if ``model`` isn't registered here. By design:
    a missing entry means we don't know if the model is tenant data or
    not, and silently returning unscoped rows would be a bug.
    """
    if model in _DIRECT_SCOPED:
        return query.where(_DIRECT_SCOPED[model] == org_id)

    if model in _TRANSITIVE_SCOPED:
        parent_cls, child_fk, parent_org_col = _TRANSITIVE_SCOPED[model]
        return query.join(parent_cls, child_fk == parent_cls.id).where(
            parent_org_col == org_id
        )

    raise KeyError(
        f"{model.__name__} is not registered in scoping rules. "
        f"If it's tenant data, add it to _DIRECT_SCOPED or _TRANSITIVE_SCOPED. "
        f"If it's truly org-agnostic (e.g. admin-only), query it directly "
        f"and audit the callsite."
    )


__all__ = ["scoped"]
