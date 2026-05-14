"""Architect/builder/reviewer trio lifecycle orchestration.

``run_trio_parent`` is the entry point: it drives a parent task through
its trio phases (architecting → awaiting_builder → architect_checkpoint),
loops until the backlog is drained, and opens the final integration PR
back to the target branch.

Phase transitions are persisted on ``Task.trio_phase`` so an external
observer (UI, debugging) can see where in the cycle we are. The outer
status transitions (``TRIO_EXECUTING → PR_CREATED`` / ``→ BLOCKED``) go
through ``orchestrator.state_machine.transition`` to enforce the
allowed-transitions check and log a ``TaskHistory`` row.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select

from agent.lifecycle.trio import architect, dispatcher
from orchestrator.state_machine import transition
from shared.database import async_session
from shared.models import Task, TaskStatus, TrioPhase

log = structlog.get_logger()


async def _set_trio_phase(parent_id: int, phase: TrioPhase | None) -> None:
    """Load the parent, set ``trio_phase``, commit. Avoids stale refs."""
    async with async_session() as s:
        p = (
            await s.execute(select(Task).where(Task.id == parent_id))
        ).scalar_one()
        p.trio_phase = phase
        await s.commit()


async def _resolve_target_branch(parent_id: int) -> str:
    """Pick the integration PR target branch for the parent.

    Non-freeform parents always target ``main``. Freeform parents target
    the repo's ``FreeformConfig.dev_branch`` when one exists, falling
    back to ``main`` otherwise.
    """
    from shared.models import FreeformConfig

    async with async_session() as s:
        p = (
            await s.execute(select(Task).where(Task.id == parent_id))
        ).scalar_one()
        if not p.freeform_mode or p.repo_id is None:
            return "main"
        cfg = (
            await s.execute(
                select(FreeformConfig).where(FreeformConfig.repo_id == p.repo_id)
            )
        ).scalar_one_or_none()
        if cfg is None:
            return "main"
        return cfg.dev_branch or "main"


async def _open_integration_pr(parent: Task, target_branch: str) -> str:
    """Open the final ``trio/<parent_id> → target_branch`` PR via gh CLI.

    The integration branch already lives on the remote (every child PR
    merge pushed to it), so we do not push from here. We just shell out
    to ``gh pr create``, using the same auth pattern Task 12 used for
    the init PR (``shared.github_auth.get_github_token`` →
    ``GH_TOKEN`` env). Returns the PR URL (gh prints it on stdout).

    Best-effort: if gh fails or no auth is available, returns an empty
    string and logs the failure rather than crashing the orchestrator.
    """
    from agent import sh
    from shared.github_auth import get_github_token

    integration_branch = f"trio/{parent.id}"

    try:
        token = await get_github_token(
            user_id=parent.created_by_user_id,
            organization_id=parent.organization_id,
        )
    except Exception as e:  # pragma: no cover — env-dependent
        log.warning(
            "trio.parent.gh_token_unavailable",
            parent_id=parent.id, error=str(e),
        )
        return ""

    gh_env = {"GH_TOKEN": token} if token else {}

    title = f"trio: integration — {parent.title}"
    body = (
        f"Final integration PR for trio parent #{parent.id}.\n\n"
        "Contains every child PR that landed on the integration branch "
        f"`{integration_branch}` during the trio cycle."
    )

    create_res = await sh.run(
        [
            "gh", "pr", "create",
            "--base", target_branch,
            "--head", integration_branch,
            "--title", title,
            "--body", body,
        ],
        timeout=30,
        env=gh_env,
    )
    if create_res.failed:
        log.warning(
            "trio.parent.integration_pr_create_failed",
            parent_id=parent.id,
            stderr=(create_res.stderr or "")[:500],
        )
        return ""
    return (create_res.stdout or "").strip()


async def _block_parent(parent_id: int, message: str) -> None:
    """Transition parent to BLOCKED and clear trio_phase. Helper used by
    every terminal failure path in ``run_trio_parent``."""
    async with async_session() as s:
        p = (
            await s.execute(select(Task).where(Task.id == parent_id))
        ).scalar_one()
        await transition(s, p, TaskStatus.BLOCKED, message=message)
        p.trio_phase = None
        await s.commit()


async def _mark_item_done(parent_id: int, item_id: str, head_sha: str | None) -> None:
    """Set an item's status to 'done' in tasks.trio_backlog. JSONB requires a
    fresh list reference for SQLAlchemy to mark the column dirty."""
    async with async_session() as s:
        p = (
            await s.execute(select(Task).where(Task.id == parent_id))
        ).scalar_one()
        backlog = list(p.trio_backlog or [])
        for i, item in enumerate(backlog):
            if item.get("id") == item_id:
                new_item = dict(item)
                new_item["status"] = "done"
                if head_sha:
                    new_item["head_sha"] = head_sha
                backlog[i] = new_item
                break
        p.trio_backlog = backlog
        await s.commit()


async def _replace_backlog_item(
    parent_id: int, old_item_id: str, new_items: list[dict],
) -> None:
    """Replace a single backlog item with one or more new items. Used by
    the architect's ``revise_backlog`` tiebreak decision."""
    async with async_session() as s:
        p = (
            await s.execute(select(Task).where(Task.id == parent_id))
        ).scalar_one()
        backlog = list(p.trio_backlog or [])
        out: list[dict] = []
        replaced = False
        for item in backlog:
            if not replaced and item.get("id") == old_item_id:
                for ni in new_items:
                    ni = dict(ni)
                    ni.setdefault("status", "pending")
                    out.append(ni)
                replaced = True
            else:
                out.append(item)
        p.trio_backlog = out
        await s.commit()


async def _ensure_integration_branch_checked_out(workspace: str, parent_id: int) -> None:
    """Make sure the workspace is on ``trio/<parent_id>``.

    The architect's initial pass may have left the workspace on a sub-branch
    (``trio/<parent_id>/init``) after ``_commit_and_open_initial_pr``. The
    dispatcher operates on the integration branch, so we explicitly check
    it out before each item.
    """
    from agent import sh

    integration_branch = f"trio/{parent_id}"
    res = await sh.run(
        ["git", "checkout", integration_branch],
        cwd=workspace, timeout=30,
    )
    if res.failed:
        log.warning(
            "trio.parent.checkout_integration_failed",
            parent_id=parent_id,
            stderr=(res.stderr or "")[:300],
        )


async def run_trio_parent(
    parent: Task,
    *,
    repair_context: dict | None = None,
) -> None:
    """Drive a parent task through the trio cycle, open the final PR.

    Fresh entry (``repair_context is None``) runs the architect's
    initial pass. Re-entry from a failed integration PR threads the CI
    log into a checkpoint pass so the architect can add fix work items.

    Per ADR-013 the per-item loop no longer creates child Task rows. It
    invokes :mod:`agent.lifecycle.trio.dispatcher`, which runs coder
    and reviewer subagents inside the parent's slot. The dispatcher
    returns an ``ItemResult``; this function persists backlog updates
    and acts on architect tiebreak decisions.
    """
    from agent.lifecycle.trio.architect import _prepare_parent_workspace

    if repair_context is not None:
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECT_CHECKPOINT)
        await architect.checkpoint(parent.id, repair_context=repair_context)
    else:
        # Idempotent re-entry: only run the initial architect pass when
        # the parent has no backlog yet. Otherwise we'd overwrite the
        # existing one on every recovery, blowing away the dispatcher's
        # progress. After a crash or a manual unblock we want to resume
        # the per-item loop from where we were, not restart.
        async with async_session() as _s:
            _p = (
                await _s.execute(select(Task).where(Task.id == parent.id))
            ).scalar_one()
            has_backlog = bool(_p.trio_backlog)
        if not has_backlog:
            await _set_trio_phase(parent.id, TrioPhase.ARCHITECTING)
            await architect.run_initial(parent.id)
        else:
            log.info(
                "trio.parent.resume_skipping_run_initial",
                parent_id=parent.id,
            )

    # Resolve once per cycle — re-cloning per item is wasteful and the
    # subagents share the workspace.
    parent_workspace: str | None = None
    repo_name: str | None = None
    home_dir: str | None = None
    org_id: int | None = None

    while True:
        # Re-read the backlog each iteration in case the architect or a
        # tiebreak revised it.
        async with async_session() as s:
            p = (
                await s.execute(select(Task).where(Task.id == parent.id))
            ).scalar_one()
            if p.status == TaskStatus.BLOCKED:
                return
            if p.status == TaskStatus.AWAITING_CLARIFICATION:
                log.info(
                    "trio.parent.paused_for_clarification",
                    parent_id=parent.id,
                )
                return
            backlog = list(p.trio_backlog or [])
            pending = [(idx, it) for idx, it in enumerate(backlog) if it.get("status") == "pending"]
            if not pending:
                break
            # Cache invariants we'll need outside the session.
            if parent_workspace is None:
                from agent.lifecycle.factory import home_dir_for_task
                parent_workspace = await _prepare_parent_workspace(p)
                repo_name = p.repo.name if p.repo else None
                home_dir = await home_dir_for_task(p)
                org_id = p.organization_id

        _, item = pending[0]
        item_id = item.get("id", "(unknown)")

        await _set_trio_phase(parent.id, TrioPhase.AWAITING_BUILDER)
        await _ensure_integration_branch_checked_out(parent_workspace, parent.id)

        result = await dispatcher.dispatch_item(
            parent_task_id=parent.id,
            work_item=item,
            workspace=parent_workspace,
            repo_name=repo_name,
            home_dir=home_dir,
            org_id=org_id,
        )

        if result.ok:
            await _mark_item_done(parent.id, item_id, result.head_sha)
            log.info(
                "trio.parent.item_done",
                parent_id=parent.id, item_id=item_id, head_sha=result.head_sha,
            )
            # No per-item architect checkpoint — the reviewer subagent is
            # the per-item quality gate. Architect runs once after the
            # whole backlog drains (below) to sanity-check the integration.
            continue

        if not result.needs_tiebreak:
            # Terminal failure — coder produced no diff after 3 tries.
            await _block_parent(
                parent.id,
                f"trio: item {item_id} terminated — {result.failure_reason}",
            )
            log.info(
                "trio.parent.blocked_on_item",
                parent_id=parent.id,
                item_id=item_id,
                reason=result.failure_reason,
            )
            return

        # Coder↔reviewer didn't converge → architect tiebreak.
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECTING)
        decision = await dispatcher.architect_tiebreak(
            parent_task_id=parent.id,
            work_item=item,
            transcript=result.transcript,
            workspace=parent_workspace,
            repo_name=repo_name,
            home_dir=home_dir,
            org_id=org_id,
        )
        action = decision.get("action", "clarify")
        log.info(
            "trio.parent.tiebreak_decision",
            parent_id=parent.id, item_id=item_id, action=action,
            reason=decision.get("reason"),
        )

        if action == "accept":
            head_sha = result.head_sha
            await _mark_item_done(parent.id, item_id, head_sha)
            continue

        if action == "redo":
            # Architect gave specific guidance; keep the item pending but
            # append the architect's guidance to its description so the
            # next coder run picks it up. Cap at one redo per item to
            # avoid loops — second tiebreak on the same item escalates.
            guidance = str(decision.get("guidance", "")).strip()
            async with async_session() as s:
                p = (
                    await s.execute(select(Task).where(Task.id == parent.id))
                ).scalar_one()
                new_backlog = list(p.trio_backlog or [])
                for i, it in enumerate(new_backlog):
                    if it.get("id") == item_id:
                        bumped = dict(it)
                        if bumped.get("architect_redo_count", 0) >= 1:
                            # Second tiebreak on the same item — give up.
                            await _block_parent(
                                parent.id,
                                f"trio: item {item_id} stuck after two architect tiebreaks",
                            )
                            return
                        bumped["architect_redo_count"] = bumped.get("architect_redo_count", 0) + 1
                        if guidance:
                            bumped["description"] = (
                                (bumped.get("description") or "")
                                + "\n\n## Architect tiebreak guidance\n"
                                + guidance
                            )
                        new_backlog[i] = bumped
                        break
                p.trio_backlog = new_backlog
                await s.commit()
            continue

        if action == "revise_backlog":
            new_items = decision.get("new_items") or []
            if not new_items:
                await _block_parent(
                    parent.id,
                    f"trio: revise_backlog tiebreak produced no new_items for {item_id}",
                )
                return
            await _replace_backlog_item(parent.id, item_id, new_items)
            continue

        if action == "clarify":
            # Tiebreak escalation to a human. The clarification-resume path
            # (architect.resume) re-enters architect.run_initial/checkpoint
            # via a saved Session — we don't have a saved tiebreak session,
            # so we block the parent with the question rather than emit a
            # resumable clarification. Operators see the BLOCKED status +
            # question and can re-trigger the parent after editing the
            # backlog item if needed. Real clarification resume for
            # tiebreaks is a follow-up.
            question = str(
                decision.get("question")
                or "Trio is stuck; please advise."
            )
            await _block_parent(
                parent.id,
                f"trio: tiebreak escalation on item {item_id} — {question[:300]}",
            )
            return

        # Unknown action — fail safe and block.
        await _block_parent(
            parent.id,
            f"trio: tiebreak returned unknown action '{action}'",
        )
        return

    # Backlog drained — architect runs one final checkpoint over the
    # integration branch's full diff before we open the integration PR.
    # If it returns ``revise`` we loop back into the per-item phase; if
    # ``done`` we open the PR; otherwise the parent is blocked.
    await _set_trio_phase(parent.id, TrioPhase.ARCHITECT_CHECKPOINT)
    decision = await architect.checkpoint(parent.id)
    action = decision.get("action", "done")

    if action == "revise":
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECTING)
        await architect.run_revision(parent.id)
        # The revision may have appended new pending items — re-enter the
        # caller (recovery will pick the parent up again on next dispatch).
        # Simplest correct thing: recurse.
        return await run_trio_parent(parent)
    if action == "awaiting_clarification":
        log.info(
            "trio.parent.paused_for_clarification_at_final_checkpoint",
            parent_id=parent.id,
        )
        return
    if action == "blocked":
        await _block_parent(
            parent.id,
            f"trio: final checkpoint blocked — {decision.get('reason', '')}",
        )
        return
    # action in {"done", "continue"} → open the integration PR.

    target = await _resolve_target_branch(parent.id)

    async with async_session() as s:
        p = (
            await s.execute(select(Task).where(Task.id == parent.id))
        ).scalar_one()
        pr_url = await _open_integration_pr(p, target)
        p.pr_url = pr_url or None
        p.trio_phase = None
        await transition(s, p, TaskStatus.PR_CREATED, message="trio: integration PR opened")
        await s.commit()

    log.info(
        "trio.parent.opened_final_pr",
        parent_id=parent.id,
        pr_url=pr_url,
        target_branch=target,
    )
