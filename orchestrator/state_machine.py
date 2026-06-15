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
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.DONE,
        TaskStatus.BLOCKED_ON_AUTH,
        TaskStatus.BLOCKED_ON_QUOTA,
        TaskStatus.TRIO_EXECUTING,
    },
    TaskStatus.PLANNING: {
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.AWAITING_PLAN_APPROVAL,  # ADR-015 §5 Phase 5 — complex-flow gate
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.BLOCKED_ON_QUOTA,
    },
    TaskStatus.AWAITING_APPROVAL: {TaskStatus.CODING, TaskStatus.PLANNING},  # approved or revision
    TaskStatus.AWAITING_PLAN_APPROVAL: {  # ADR-015 §5 Phase 5
        TaskStatus.CODING,  # approved
        TaskStatus.BLOCKED,  # rejected
        TaskStatus.PLANNING,  # plan revision requested (future use)
    },
    TaskStatus.AWAITING_CLARIFICATION: {
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.FAILED,
        TaskStatus.TRIO_EXECUTING,  # NEW — trio architect resume
    },  # user replied
    TaskStatus.CODING: {
        TaskStatus.VERIFYING,  # freeform self-verification gate
        TaskStatus.PR_CREATED,
        TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.DONE,
        TaskStatus.BLOCKED_ON_QUOTA,
        TaskStatus.TRIO_REVIEW,
    },
    TaskStatus.VERIFYING: {  # freeform self-verification
        TaskStatus.PR_CREATED,
        TaskStatus.CODING,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED_ON_QUOTA,
        TaskStatus.TRIO_REVIEW,
    },
    TaskStatus.PR_CREATED: {
        TaskStatus.AWAITING_CI,
        TaskStatus.PR_REVIEW,  # ADR-015 §5 — self-PR-review gate (simple flow)
        TaskStatus.AWAITING_REVIEW,  # ADR-017 — trio falls through immediately; AWAITING_REVIEW is the long-lived "PR open" state
    },
    TaskStatus.PR_REVIEW: {  # ADR-015 §5 — verdict pass→DONE, fail→BLOCKED
        TaskStatus.DONE,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.ADDRESSING_COMMENTS,  # ADR-015 §5 Phase 5 — artefact-scope comments to address
    },
    TaskStatus.ADDRESSING_COMMENTS: {  # ADR-015 §5 Phase 5 — single round of self-fixups
        TaskStatus.DONE,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.AWAITING_CI: {
        TaskStatus.AWAITING_REVIEW,
        TaskStatus.CODING,
        TaskStatus.FAILED,
        TaskStatus.TRIO_EXECUTING,
    },  # CI pass/fail
    TaskStatus.AWAITING_REVIEW: {
        TaskStatus.DONE,
        TaskStatus.CODING,
        TaskStatus.BLOCKED,
        TaskStatus.ITERATING,
    },  # approved, changes, cycle-2 failure, or ADR-017 iteration feedback
    # ADR-017 — trio iteration: user gives PR feedback → ITERATING; architect
    # appends new backlog items; per-item loop drains → AWAITING_REVIEW again.
    # Also allow ITERATING → DONE for merge-during-iteration race (GitHub webhook).
    TaskStatus.ITERATING: {TaskStatus.AWAITING_REVIEW, TaskStatus.BLOCKED, TaskStatus.DONE},
    TaskStatus.BLOCKED: {
        TaskStatus.CODING,
        TaskStatus.PLANNING,
        TaskStatus.FAILED,
        TaskStatus.DONE,
        # Recovery: allow re-driving a stuck trio child after a dispatcher
        # fix lands. Observed on harpoon task #25 (2026-05-24) — coder
        # repeatedly produced no diff because the architect's items were
        # already-done state changes; without BLOCKED → TRIO_EXECUTING we
        # had no way to re-attempt with the patched no-diff escalation.
        TaskStatus.TRIO_EXECUTING,
    },
    TaskStatus.BLOCKED_ON_AUTH: {
        TaskStatus.QUEUED,
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.FAILED,
    },
    TaskStatus.BLOCKED_ON_QUOTA: {
        TaskStatus.QUEUED,
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.FAILED,
    },
    TaskStatus.TRIO_EXECUTING: {
        TaskStatus.PR_CREATED,
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_CLARIFICATION,  # NEW — architect needs answers
        # ADR-015 §2 / Phase 6 — design-doc gate enters from TRIO_EXECUTING.
        TaskStatus.ARCHITECT_DESIGNING,
        # ADR-015 §4 / Phase 7 — backlog drained → final review.
        TaskStatus.FINAL_REVIEW,
    },
    TaskStatus.TRIO_REVIEW: {TaskStatus.PR_CREATED, TaskStatus.CODING, TaskStatus.BLOCKED},
    # ADR-015 §2 / Phase 6 — design + backlog-emit chain for complex_large.
    TaskStatus.ARCHITECT_DESIGNING: {
        TaskStatus.AWAITING_DESIGN_APPROVAL,
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_CLARIFICATION,
    },
    TaskStatus.AWAITING_DESIGN_APPROVAL: {
        TaskStatus.ARCHITECT_BACKLOG_EMIT,  # approved
        TaskStatus.BLOCKED,  # rejected
        TaskStatus.ARCHITECT_DESIGNING,  # re-design requested (future use)
    },
    TaskStatus.ARCHITECT_BACKLOG_EMIT: {
        TaskStatus.TRIO_EXECUTING,  # backlog emitted → builder dispatch
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.ARCHITECT_DESIGNING,  # validator rejected → re-design
        # ADR-015 §9 / Phase 8 — architect spawned sub-architects instead
        # of emitting a flat backlog.
        TaskStatus.AWAITING_SUB_ARCHITECTS,
    },
    # ADR-015 §9 / Phase 8 — parent task paused while sub-architects run
    # serially. All-done → FINAL_REVIEW (same path as a flat backlog drain);
    # any slice failed permanently → BLOCKED.
    TaskStatus.AWAITING_SUB_ARCHITECTS: {
        TaskStatus.FINAL_REVIEW,
        TaskStatus.BLOCKED,
    },
    # ADR-015 §4 / Phase 7 — final review + architect gap-fix loop.
    TaskStatus.FINAL_REVIEW: {
        TaskStatus.PR_CREATED,  # verdict=passed → PR creation path
        TaskStatus.ARCHITECT_GAP_FIX,  # verdict=gaps_found → architect resumes
        TaskStatus.BLOCKED,  # exhausted gap-fix rounds
    },
    TaskStatus.ARCHITECT_GAP_FIX: {
        TaskStatus.TRIO_EXECUTING,  # architect dispatched new items
        TaskStatus.BLOCKED,  # architect escalated or out of rounds
        # Retry path: a malformed/empty gap-fix decision (action=None) loops
        # back to final review for another bounded round instead of blocking.
        TaskStatus.FINAL_REVIEW,
    },
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
        # ADR-019 T7 — after all ADRs resolved, park here until required
        # secrets are populated. Direct → DISPATCHING_DOMAIN_BUILDS still
        # allowed when there are no architect-required secrets at all.
        TaskStatus.AWAITING_REQUIRED_SECRETS,
    },
    # ADR-019 T7 — intermediate gate status: every architect-required row
    # must have a non-null value_enc before Phase D can run. Auto-unblocked
    # by PUT /repos/{id}/secrets/{k} when the last missing secret is set.
    TaskStatus.AWAITING_REQUIRED_SECRETS: {
        TaskStatus.DISPATCHING_DOMAIN_BUILDS,  # all secrets now populated
        TaskStatus.BLOCKED,                    # manual cancel path
    },
    TaskStatus.DISPATCHING_DOMAIN_BUILDS: {
        TaskStatus.BUILDING_DOMAINS,
        # Recovery: a zero-child dispatch (e.g. root ADR missing from the
        # workspace) has nothing to wait for, so re-enter final verification
        # instead of deadlocking in BUILDING_DOMAINS. Scaffold #329, 2026-06-14.
        TaskStatus.AWAITING_FINAL_VERIFICATION,
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
    session.add(
        TaskHistory(
            task_id=task.id,
            from_status=from_status,
            to_status=to_status,
            message=message,
        )
    )
    await session.flush()
    return task


async def get_task(session: AsyncSession, task_id: int) -> Task | None:
    result = await session.execute(select(Task).where(Task.id == task_id))
    return result.scalar_one_or_none()
