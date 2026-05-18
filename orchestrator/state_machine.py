"""Task state machine — enforces valid transitions and logs history."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Task, TaskHistory, TaskStatus

# Valid state transitions
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.INTAKE: {
        TaskStatus.CLASSIFYING,
        # ADR-018 — SCAFFOLD parent flow skips QUEUED and enters the intent
        # grill directly after classification (or directly from intake when
        # the complexity is set inline). Complexity gating happens at the
        # caller, not in the transition dict.
        TaskStatus.AWAITING_INTENT_GRILL,
    },
    TaskStatus.CLASSIFYING: {
        TaskStatus.QUEUED,
        TaskStatus.FAILED,
        # ADR-018 — SCAFFOLD task classified → straight into intent grill.
        TaskStatus.AWAITING_INTENT_GRILL,
    },
    TaskStatus.QUEUED: {
        TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.DONE,
        TaskStatus.BLOCKED_ON_AUTH, TaskStatus.BLOCKED_ON_QUOTA,
    },
    TaskStatus.PLANNING: {
        TaskStatus.AWAITING_APPROVAL, TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.BLOCKED_ON_QUOTA,
    },
    TaskStatus.AWAITING_APPROVAL: {TaskStatus.CODING, TaskStatus.PLANNING},  # approved or revision
    TaskStatus.AWAITING_CLARIFICATION: {TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.FAILED},  # user replied
    TaskStatus.CODING: {
        TaskStatus.VERIFYING,                                          # freeform self-verification gate
        TaskStatus.PR_CREATED, TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.DONE,
        TaskStatus.BLOCKED_ON_QUOTA,
    },
    TaskStatus.VERIFYING: {                                            # freeform self-verification
        TaskStatus.PR_CREATED,
        TaskStatus.CODING,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED_ON_QUOTA,
    },
    TaskStatus.PR_CREATED: {TaskStatus.AWAITING_CI},
    TaskStatus.AWAITING_CI: {TaskStatus.AWAITING_REVIEW, TaskStatus.CODING, TaskStatus.FAILED},  # CI pass/fail
    TaskStatus.AWAITING_REVIEW: {TaskStatus.DONE, TaskStatus.CODING, TaskStatus.BLOCKED},  # approved, changes, or cycle-2 failure
    TaskStatus.BLOCKED: {TaskStatus.CODING, TaskStatus.PLANNING, TaskStatus.FAILED, TaskStatus.DONE},
    TaskStatus.BLOCKED_ON_AUTH: {TaskStatus.QUEUED, TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.FAILED},
    TaskStatus.BLOCKED_ON_QUOTA: {TaskStatus.QUEUED, TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.FAILED},
    # ADR-018 — SCAFFOLD parent state machine. Intent grill → root ADR
    # gate → per-domain ADR gate → dispatch per-domain trios → final
    # verification. Complexity gating is enforced at the caller; the
    # transition dict only models the legal moves.
    TaskStatus.AWAITING_INTENT_GRILL: {
        TaskStatus.BUILDING_ROOT_ADR,
    },
    TaskStatus.BUILDING_ROOT_ADR: {
        TaskStatus.AWAITING_ROOT_ADR_APPROVAL,
    },
    TaskStatus.AWAITING_ROOT_ADR_APPROVAL: {
        TaskStatus.BUILDING_DOMAIN_ADRS,  # verdict=approved
        TaskStatus.BUILDING_ROOT_ADR,  # verdict=revise — resume architect session
        TaskStatus.BLOCKED,  # rejected OR 3 revise rounds exhausted
    },
    TaskStatus.BUILDING_DOMAIN_ADRS: {
        # ADR-018 Stage 8 — per-domain grill round pauses the parent here
        # while the user (or PO standin) answers the grill agent's
        # pending question. Re-entry transitions back into
        # BUILDING_DOMAIN_ADRS once the answer lands.
        TaskStatus.AWAITING_DOMAIN_GRILL,
        TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL,
    },
    TaskStatus.AWAITING_DOMAIN_GRILL: {
        TaskStatus.BUILDING_DOMAIN_ADRS,  # user answered — resume grill
        TaskStatus.BLOCKED,  # rare — escalation / unanswerable question
    },
    TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL: {
        TaskStatus.BUILDING_DOMAIN_ADRS,  # any domain marked revise
        TaskStatus.DISPATCHING_DOMAIN_BUILDS,  # all approved/rejected — no revise left
    },
    TaskStatus.DISPATCHING_DOMAIN_BUILDS: {
        TaskStatus.BUILDING_DOMAINS,
    },
    TaskStatus.BUILDING_DOMAINS: {
        TaskStatus.AWAITING_FINAL_VERIFICATION,  # all children terminal
    },
    TaskStatus.AWAITING_FINAL_VERIFICATION: {
        TaskStatus.DONE,  # verify passed
        TaskStatus.DISPATCHING_DOMAIN_BUILDS,  # gaps_found — spawn fix children
        TaskStatus.BLOCKED,  # 3 verify rounds exhausted
    },
    TaskStatus.DONE: set(),
    TaskStatus.FAILED: {TaskStatus.DONE},
}


class InvalidTransition(Exception):
    pass


async def transition(
    session: AsyncSession,
    task: Task,
    to_status: TaskStatus,
    message: str = "",
) -> Task:
    """Move a task to a new status, validating the transition and logging history."""
    from_status = task.status
    allowed = TRANSITIONS.get(from_status, set())

    if to_status not in allowed:
        raise InvalidTransition(
            f"Cannot transition from {from_status.value} to {to_status.value}. "
            f"Allowed: {[s.value for s in allowed]}"
        )

    task.status = to_status
    session.add(TaskHistory(
        task_id=task.id,
        from_status=from_status,
        to_status=to_status,
        message=message,
    ))
    await session.flush()
    return task


async def get_task(session: AsyncSession, task_id: int) -> Task | None:
    result = await session.execute(select(Task).where(Task.id == task_id))
    return result.scalar_one_or_none()
