"""Scaffold parent state-machine driver — ADR-018.

``run_scaffold_parent`` is the entry point. Given a SCAFFOLD parent
task, it dispatches the right phase function based on ``task.status``,
transitions to the next status, and either loops (synchronous next
phase) or returns (waiting on an external gate or child fan-out).

The driver is **re-entrant**. Every place that opens an external gate
(root-ADR approval, domain-ADR approvals, child fan-out) returns from
this function; an event handler in ``run.py`` re-invokes it once the
external signal lands.

Round counters for the project-level final-verification loop are stored
in ``Task.subtasks`` (existing JSONB column) under the
``"scaffold.final_verify_rounds"`` key. We don't add a new column —
Stage 1 explicitly said not to.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from agent.lifecycle.scaffold import (
    dispatch_children,
    domain_architect,
    final_verification,
    intent_grill,
    root_architect,
)
from orchestrator.state_machine import transition
from shared.database import async_session
from shared.models import Task, TaskStatus

log = structlog.get_logger()


MAX_FINAL_VERIFY_ROUNDS = 3


# ---------------------------------------------------------------------------
# Round-counter helpers — backed by Task.subtasks JSONB.
# ---------------------------------------------------------------------------


_SCAFFOLD_KEY = "scaffold"
_FINAL_VERIFY_KEY = "final_verify_rounds"


def _get_final_verify_rounds(task: Task) -> int:
    bucket = (task.subtasks or {}) if isinstance(task.subtasks, dict) else {}
    scaffold = bucket.get(_SCAFFOLD_KEY) if isinstance(bucket, dict) else None
    if not isinstance(scaffold, dict):
        return 0
    raw = scaffold.get(_FINAL_VERIFY_KEY)
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _bump_final_verify_rounds(task: Task) -> int:
    current = _get_final_verify_rounds(task)
    new_value = current + 1
    bucket = task.subtasks if isinstance(task.subtasks, dict) else {}
    new_bucket = dict(bucket)
    scaffold = new_bucket.get(_SCAFFOLD_KEY) or {}
    if not isinstance(scaffold, dict):
        scaffold = {}
    new_scaffold = dict(scaffold)
    new_scaffold[_FINAL_VERIFY_KEY] = new_value
    new_bucket[_SCAFFOLD_KEY] = new_scaffold
    task.subtasks = new_bucket
    return new_value


# ---------------------------------------------------------------------------
# Transition helper — open a session, transition, commit, reload.
# ---------------------------------------------------------------------------


async def _transition_and_reload(task_id: int, to_status: TaskStatus, *, message: str = "") -> Task:
    """Run a state-machine transition and return a freshly-loaded Task.

    The driver always operates on a reloaded row so concurrent writes
    (e.g. router endpoints stamping verdicts) don't trip stale-state
    bugs.
    """

    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        await transition(s, task, to_status, message=message)
        await s.commit()
    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
    return task


async def _reload(task_id: int) -> Task:
    async with async_session() as s:
        return (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def run_scaffold_parent(task: Task) -> None:
    """Drive ``task`` through the next available scaffold phase(s).

    Loops while phases can run synchronously; returns when the next
    transition requires an external signal (gate verdict, child fan-out
    completion).
    """

    while True:
        status = task.status

        if status == TaskStatus.AWAITING_INTENT_GRILL:
            log.info("scaffold.parent.phase_a_intent_grill", task_id=task.id)
            await intent_grill.run(task)
            task = await _transition_and_reload(
                task.id,
                TaskStatus.BUILDING_ROOT_ADR,
                message="Intent grill complete; building root ADR",
            )
            continue

        if status == TaskStatus.BUILDING_ROOT_ADR:
            log.info("scaffold.parent.phase_b_root_architect", task_id=task.id)
            await root_architect.run(task)
            task = await _transition_and_reload(
                task.id,
                TaskStatus.AWAITING_ROOT_ADR_APPROVAL,
                message="Root ADR written; awaiting approval",
            )
            return  # External gate — router re-invokes us after verdict.

        if status == TaskStatus.AWAITING_ROOT_ADR_APPROVAL:
            # The verdict endpoint transitions us out of this state. If
            # we land here in the driver loop, the gate is still open —
            # nothing to do.
            log.info(
                "scaffold.parent.awaiting_root_adr_approval",
                task_id=task.id,
            )
            return

        if status == TaskStatus.BUILDING_DOMAIN_ADRS:
            log.info("scaffold.parent.phase_c_domain_architects", task_id=task.id)
            outcome = await domain_architect.run(task)
            # Stage 8: the domain loop is now (grill → architect) per domain.
            # On a grill pause we park the parent in AWAITING_DOMAIN_GRILL
            # and return — the router endpoint re-invokes us once the user
            # (or PO standin) answers. The progress index is persisted on
            # task.subtasks so re-entry resumes on the right domain.
            outcome_status = (
                (outcome or {}).get("status") if isinstance(outcome, dict) else None
            )
            if outcome_status == "awaiting_grill":
                slug = (outcome or {}).get("domain_slug") or ""
                task = await _transition_and_reload(
                    task.id,
                    TaskStatus.AWAITING_DOMAIN_GRILL,
                    message=(
                        f"Domain grill paused on `{slug}`; awaiting user answer"
                        if slug
                        else "Domain grill paused; awaiting user answer"
                    ),
                )
                return  # External signal — router re-invokes us on answer.

            # outcome_status == "all_complete" (or legacy/unknown) — advance
            # to the per-domain ADR approval gate.
            task = await _transition_and_reload(
                task.id,
                TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL,
                message="Domain ADRs written; awaiting per-domain approvals",
            )
            return  # External gate.

        if status == TaskStatus.AWAITING_DOMAIN_GRILL:
            # External gate — the router endpoint transitions us back to
            # BUILDING_DOMAIN_ADRS once the user answers, then re-invokes
            # the driver. If we land here in the driver loop the gate is
            # still open: nothing to do.
            log.info(
                "scaffold.parent.awaiting_domain_grill",
                task_id=task.id,
            )
            return

        if status == TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL:
            log.info(
                "scaffold.parent.awaiting_domain_adr_approval",
                task_id=task.id,
            )
            return

        if status == TaskStatus.DISPATCHING_DOMAIN_BUILDS:
            log.info("scaffold.parent.phase_d_dispatch", task_id=task.id)
            await dispatch_children.run(task)
            task = await _transition_and_reload(
                task.id,
                TaskStatus.BUILDING_DOMAINS,
                message="Child trios dispatched; waiting for them to complete",
            )
            return  # External signal — on_task_finished fans us back in
            # once every child reaches a terminal state.

        if status == TaskStatus.BUILDING_DOMAINS:
            # Children are running. The finished-fan-in handler will
            # transition us to AWAITING_FINAL_VERIFICATION when all
            # children are terminal.
            log.info("scaffold.parent.building_domains", task_id=task.id)
            return

        if status == TaskStatus.AWAITING_FINAL_VERIFICATION:
            log.info("scaffold.parent.phase_e_final_verification", task_id=task.id)
            # Bump the round counter BEFORE we run so the gaps_found
            # branch can short-circuit cleanly when we're already at cap.
            async with async_session() as s:
                live = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
                rounds = _bump_final_verify_rounds(live)
                await s.commit()

            verdict = await final_verification.run(task)

            if verdict == "passed":
                task = await _transition_and_reload(
                    task.id,
                    TaskStatus.DONE,
                    message="Final verification passed",
                )
                return

            if verdict == "gaps_found":
                if rounds >= MAX_FINAL_VERIFY_ROUNDS:
                    task = await _transition_and_reload(
                        task.id,
                        TaskStatus.BLOCKED,
                        message=(
                            f"Final verification still has gaps after "
                            f"{MAX_FINAL_VERIFY_ROUNDS} rounds"
                        ),
                    )
                    return

                task = await _transition_and_reload(
                    task.id,
                    TaskStatus.DISPATCHING_DOMAIN_BUILDS,
                    message=(
                        f"Final verification round {rounds} found gaps; dispatching fix children"
                    ),
                )
                continue

            # Unknown verdict — fail safe.
            task = await _transition_and_reload(
                task.id,
                TaskStatus.BLOCKED,
                message=f"Final verification returned unknown verdict '{verdict}'",
            )
            return

        # Unexpected status: log and bail. Don't crash — a misrouted
        # invocation (e.g. against a non-SCAFFOLD task) should be a
        # no-op, not a state-machine corruption.
        log.warning(
            "scaffold.parent.unexpected_status",
            task_id=task.id,
            status=status.value if hasattr(status, "value") else str(status),
        )
        return


__all__ = [
    "MAX_FINAL_VERIFY_ROUNDS",
    "run_scaffold_parent",
]
