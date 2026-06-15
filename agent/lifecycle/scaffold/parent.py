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

import asyncio
import json
import os

import structlog
from sqlalchemy import select

from agent.lifecycle.scaffold import (
    dispatch_children,
    domain_adr_approval,
    domain_architect,
    domain_grill,
    final_verification,
    intent_grill,
    root_adr_approval,
    root_architect,
)
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.scaffold.validators import parse_domains
from agent.lifecycle.workspace_paths import (
    ROOT_ADR_APPROVAL_PATH,
    ROOT_ADR_PATH,
    domain_grill_answer_path,
    domain_grill_question_path,
)
from orchestrator.state_machine import transition
from shared.database import async_session
from shared.events import publish, task_clarification_needed
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
# Bounded auto-retry for phase calls. Each phase call (intent_grill,
# root_architect, domain_architect, final_verification) does heavy LLM work
# and is the most likely place to fail with transient errors — TCP drops,
# subprocess spawn OSErrors (ARG_MAX), Bedrock timeouts. We wrap them so
# a single transient failure doesn't strand the whole scaffold. After
# ``_MAX_PHASE_ATTEMPTS`` the task transitions to BLOCKED with the error
# stored on ``task.error`` so the UI surfaces it instead of stalling silently.
# ---------------------------------------------------------------------------


_MAX_PHASE_ATTEMPTS = 3


async def _run_phase_with_retry(
    task_id: int,
    phase_name: str,
    coro_factory,
):
    """Call ``coro_factory()`` up to _MAX_PHASE_ATTEMPTS times.

    ``coro_factory`` is a zero-arg async-callable so each retry produces a
    fresh awaitable. Returns the phase result on success. On final failure
    transitions the parent to BLOCKED with the error stored on ``task.error``
    and re-raises so the driver loop bails.
    """

    last_exc: BaseException | None = None
    for attempt in range(1, _MAX_PHASE_ATTEMPTS + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            log.exception(
                "scaffold.parent.phase_failed",
                task_id=task_id,
                phase=phase_name,
                attempt=attempt,
                max_attempts=_MAX_PHASE_ATTEMPTS,
            )
            if attempt < _MAX_PHASE_ATTEMPTS:
                # Tiny backoff so a transient blip (e.g. token rotation, brief
                # Bedrock 503) has a moment to clear. Not exponential — we
                # cap at 3 attempts total so 1s + 2s is sufficient.
                await asyncio.sleep(attempt)

    # All attempts exhausted — park the task as BLOCKED with the error so
    # the UI surfaces it.
    err_text = f"{phase_name} failed after {_MAX_PHASE_ATTEMPTS} attempts: {last_exc}"
    async with async_session() as s:
        live = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        live.error = err_text[:2000]
        await transition(s, live, TaskStatus.BLOCKED, message=err_text[:500])
        await s.commit()
    log.error(
        "scaffold.parent.phase_blocked",
        task_id=task_id,
        phase=phase_name,
        error=str(last_exc),
    )
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Gate exit pattern — uniform across every AWAITING_* phase exit:
#   freeform_mode is True  -> dispatch the PO standin synchronously, then
#                             re-apply via the gate's apply_verdict and let
#                             the driver loop continue from the new status.
#   freeform_mode is False -> publish a CLARIFICATION_NEEDED event so the
#                             queueing user is DM'd by the Slack/Telegram
#                             integration, and return — the router's
#                             apply_verdict re-invokes the driver when the
#                             user POSTs a verdict via the UI.
# ---------------------------------------------------------------------------


async def _notify_user(task: Task, *, question: str, phase: str) -> None:
    """Publish a CLARIFICATION_NEEDED for the queueing user.

    The Slack notification_loop in integrations/slack/main.py listens for
    this event and DMs ``task.created_by_user_id``. The web UI also picks
    it up via the websocket bridge.
    """

    await publish(task_clarification_needed(task.id, question=question, phase=phase))


async def _handle_root_adr_gate_freeform(task: Task) -> None:
    """Freeform root-ADR gate: PO writes verdict file, then we apply it."""

    workspace = await prepare_scaffold_workspace(task)
    await root_adr_approval.request_po_verdict(task)
    verdict_abs = os.path.join(workspace, ROOT_ADR_APPROVAL_PATH)
    with open(verdict_abs) as fh:
        verdict_payload = json.load(fh)
    await root_adr_approval.apply_verdict(task.id, verdict_payload)


async def _handle_domain_adr_gate_freeform(task: Task) -> None:
    """Freeform per-domain ADR gate: PO writes one verdict per slug; we apply each.

    Idempotency note: ``request_po_verdicts`` writes ALL verdict files before
    returning. ``apply_verdict`` reads every file on disk to compute the
    aggregate state, so the very FIRST call below sees all-resolved and
    transitions the parent to ``AWAITING_REQUIRED_SECRETS``. Subsequent calls
    would try the same transition and trip
    ``InvalidTransition: AWAITING_REQUIRED_SECRETS -> AWAITING_REQUIRED_SECRETS``.
    We reload the task between iterations and exit the loop once the gate has
    been cleared.
    """

    verdict_paths = await domain_adr_approval.request_po_verdicts(task)
    for verdict_abs in verdict_paths:
        # Bail out early if the gate has already been cleared by a previous
        # apply_verdict call (or by a concurrent driver / router request).
        live_status = (await _reload(task.id)).status
        if live_status != TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL:
            log.info(
                "scaffold.parent.po_domain_adr_gate_already_cleared",
                task_id=task.id,
                current_status=str(live_status),
            )
            break

        try:
            with open(verdict_abs) as fh:
                verdict_payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            log.warning(
                "scaffold.parent.po_domain_adr_unreadable",
                task_id=task.id,
                path=verdict_abs,
            )
            continue
        slug = (verdict_payload or {}).get("slug") if isinstance(verdict_payload, dict) else None
        if not slug:
            log.warning(
                "scaffold.parent.po_domain_adr_missing_slug",
                task_id=task.id,
                path=verdict_abs,
            )
            continue
        await domain_adr_approval.apply_verdict(task.id, slug, verdict_payload)


async def _handle_domain_grill_gate_freeform(task: Task) -> bool:
    """Freeform domain-grill gate: find the pending domain, PO answers, transition back.

    Returns True if the answer was applied (caller should reload and continue).
    Returns False if no pending question was found (caller should park).
    """

    workspace = await prepare_scaffold_workspace(task)

    root_adr_abs = os.path.join(workspace, ROOT_ADR_PATH)
    if not os.path.isfile(root_adr_abs):
        log.warning("scaffold.parent.domain_grill_no_root_adr", task_id=task.id)
        return False
    with open(root_adr_abs) as fh:
        root_adr_md = fh.read()
    domains = parse_domains(root_adr_md)

    # Find the first domain with a pending question (question file exists
    # but no matching answer file).
    pending_slug: str | None = None
    pending_question: str | None = None
    for d in domains:
        slug = (d.get("slug") or "").strip()
        if not slug:
            continue
        q_abs = os.path.join(workspace, domain_grill_question_path(slug))
        a_abs = os.path.join(workspace, domain_grill_answer_path(slug))
        if not os.path.isfile(q_abs):
            continue
        if os.path.isfile(a_abs):
            continue
        try:
            with open(q_abs) as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        question = payload.get("question") if isinstance(payload, dict) else None
        if not question:
            continue
        pending_slug = slug
        pending_question = str(question)
        break

    if not pending_slug or not pending_question:
        log.warning("scaffold.parent.domain_grill_no_pending_question", task_id=task.id)
        return False

    await domain_grill.request_po_domain_answer(task, pending_question, pending_slug)
    # Answer is now on disk. Transition back to BUILDING_DOMAIN_ADRS so the
    # parent loop re-enters the domain architect and consumes the answer.
    async with async_session() as s:
        live = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
        await transition(
            s,
            live,
            TaskStatus.BUILDING_DOMAIN_ADRS,
            message=f"Domain grill `{pending_slug}` answered by PO standin",
        )
        await s.commit()
    return True


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
            await _run_phase_with_retry(
                task.id, "intent_grill", lambda t=task: intent_grill.run(t)
            )
            task = await _transition_and_reload(
                task.id,
                TaskStatus.BUILDING_ROOT_ADR,
                message="Intent grill complete; building root ADR",
            )
            continue

        if status == TaskStatus.BUILDING_ROOT_ADR:
            log.info("scaffold.parent.phase_b_root_architect", task_id=task.id)
            await _run_phase_with_retry(
                task.id, "root_architect", lambda t=task: root_architect.run(t)
            )
            task = await _transition_and_reload(
                task.id,
                TaskStatus.AWAITING_ROOT_ADR_APPROVAL,
                message="Root ADR written; awaiting approval",
            )
            # Fall through to AWAITING_ROOT_ADR_APPROVAL — that block
            # decides between freeform PO standin (continue) and human
            # notify-and-yield (return).
            continue

        if status == TaskStatus.AWAITING_ROOT_ADR_APPROVAL:
            log.info(
                "scaffold.parent.awaiting_root_adr_approval",
                task_id=task.id,
                freeform=bool(task.freeform_mode),
            )
            if task.freeform_mode:
                try:
                    await _handle_root_adr_gate_freeform(task)
                except Exception:
                    log.exception("scaffold.parent.po_root_adr_failed", task_id=task.id)
                    return
                task = await _reload(task.id)
                continue
            # Human-in-loop: notify the queueing user and yield.
            await _notify_user(
                task,
                question=(
                    "Root ADR is ready for review. Open the task to approve, "
                    "request a revise, or reject."
                ),
                phase="scaffold_root_adr",
            )
            return

        if status == TaskStatus.BUILDING_DOMAIN_ADRS:
            log.info("scaffold.parent.phase_c_domain_architects", task_id=task.id)
            outcome = await _run_phase_with_retry(
                task.id, "domain_architect", lambda t=task: domain_architect.run(t)
            )
            # Stage 8: the domain loop is now (grill → architect) per domain.
            # On a grill pause we park the parent in AWAITING_DOMAIN_GRILL
            # and return — the router endpoint re-invokes us once the user
            # (or PO standin) answers. The progress index is persisted on
            # task.subtasks so re-entry resumes on the right domain.
            outcome_status = (outcome or {}).get("status") if isinstance(outcome, dict) else None
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
                # Fall through to AWAITING_DOMAIN_GRILL — freeform path
                # PO-answers and loops back; human path notifies and yields.
                continue

            # outcome_status == "all_complete" (or legacy/unknown) — advance
            # to the per-domain ADR approval gate.
            task = await _transition_and_reload(
                task.id,
                TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL,
                message="Domain ADRs written; awaiting per-domain approvals",
            )
            # Fall through to AWAITING_DOMAIN_ADR_APPROVAL — freeform path
            # PO-verdicts each slug and loops back; human path notifies.
            continue

        if status == TaskStatus.AWAITING_DOMAIN_GRILL:
            log.info(
                "scaffold.parent.awaiting_domain_grill",
                task_id=task.id,
                freeform=bool(task.freeform_mode),
            )
            if task.freeform_mode:
                try:
                    answered = await _handle_domain_grill_gate_freeform(task)
                except Exception:
                    log.exception("scaffold.parent.po_domain_grill_failed", task_id=task.id)
                    return
                if not answered:
                    return
                task = await _reload(task.id)
                continue
            await _notify_user(
                task,
                question=(
                    "The domain architect paused on a clarifying question. Open the task to answer."
                ),
                phase="scaffold_domain_grill",
            )
            return

        if status == TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL:
            log.info(
                "scaffold.parent.awaiting_domain_adr_approval",
                task_id=task.id,
                freeform=bool(task.freeform_mode),
            )
            if task.freeform_mode:
                try:
                    await _handle_domain_adr_gate_freeform(task)
                except Exception:
                    log.exception("scaffold.parent.po_domain_adr_failed", task_id=task.id)
                    return
                task = await _reload(task.id)
                continue
            await _notify_user(
                task,
                question=(
                    "Per-domain ADRs are ready for review. Open the task to verdict each domain."
                ),
                phase="scaffold_domain_adr",
            )
            return

        if status == TaskStatus.AWAITING_REQUIRED_SECRETS:
            # ADR-019 T7 — secrets gate between Phase C and Phase D.
            # check_secrets_gate handles its own transition; if it returns
            # True, the task is now DISPATCHING_DOMAIN_BUILDS and we reload
            # to continue the loop. If False, the parent parks here until
            # PUT /repos/{id}/secrets/{k} or the recheck endpoint unblocks it.
            # The gate is human-required in BOTH modes (a PO standin can't
            # invent secret values), so notification fires regardless of
            # task.freeform_mode.
            log.info("scaffold.parent.awaiting_required_secrets", task_id=task.id)
            gate_passed = await dispatch_children.check_secrets_gate(task)
            if gate_passed:
                task = await _reload(task.id)
                continue
            # Parked — surface which keys are missing so the user knows
            # what to PUT. check_secrets_gate persists the list onto
            # task.subtasks.scaffold.missing_secrets — re-read after the
            # gate call so we get the freshest value.
            live = await _reload(task.id)
            missing: list[str] = []
            bucket = live.subtasks if isinstance(live.subtasks, dict) else {}
            scaffold = bucket.get(_SCAFFOLD_KEY) if isinstance(bucket, dict) else None
            if isinstance(scaffold, dict):
                raw = scaffold.get("missing_secrets") or []
                if isinstance(raw, list):
                    missing = [str(k) for k in raw if k]
            keys_blob = ", ".join(f"`{k}`" for k in missing) if missing else "(see repo settings)"
            await _notify_user(
                live,
                question=(
                    "Scaffold is paused waiting on required secrets: "
                    f"{keys_blob}. Open the repo settings to populate them."
                ),
                phase="scaffold_secrets",
            )
            return  # Parked — re-invoked by PUT hook or recheck endpoint.

        if status == TaskStatus.DISPATCHING_DOMAIN_BUILDS:
            log.info("scaffold.parent.phase_d_dispatch", task_id=task.id)
            created = await dispatch_children.run(task)
            if not created:
                # No new children were dispatched — either the root ADR is
                # missing from the workspace (a gitignored artefact lost on a
                # workspace recreate) or every approved domain child already
                # exists. Parking in BUILDING_DOMAINS would DEADLOCK: the
                # on-task-finished fan-in only fires when a child reaches a
                # terminal state, but no new child exists and any prior
                # children are already terminal. Re-enter final verification
                # instead (bounded by MAX_FINAL_VERIFY_ROUNDS, which BLOCKs
                # with a clear message if gaps persist). Scaffold #329,
                # 2026-06-14.
                log.warning("scaffold.parent.phase_d_dispatched_no_children", task_id=task.id)
                task = await _transition_and_reload(
                    task.id,
                    TaskStatus.AWAITING_FINAL_VERIFICATION,
                    message="No new domain children to dispatch; re-running final verification",
                )
                continue
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

            verdict = await _run_phase_with_retry(
                task.id, "final_verification", lambda t=task: final_verification.run(t)
            )

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
