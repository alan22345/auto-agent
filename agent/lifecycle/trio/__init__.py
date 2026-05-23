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

import os

import structlog
from sqlalchemy import select

from agent.lifecycle.standin import run_freeform_gate
from agent.lifecycle.trio import architect, design_approval, dispatcher
from agent.lifecycle.workspace_paths import DESIGN_PATH
from orchestrator.state_machine import transition
from shared.database import async_session
from shared.events import publish, task_iteration_complete, task_pr_created
from shared.models import Task, TaskComplexity, TaskStatus, TrioPhase

log = structlog.get_logger()


async def _set_trio_phase(parent_id: int, phase: TrioPhase | None) -> None:
    """Load the parent, set ``trio_phase``, commit. Avoids stale refs."""
    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent_id))).scalar_one()
        p.trio_phase = phase
        await s.commit()


def _design_md_exists(workspace_root: str | None, task_id: int) -> bool:
    """Return True iff ``.auto-agent/design.md`` exists AND its first
    non-empty line is the ``<!-- auto-agent: task_id=<task_id> -->`` header.

    ADR-015 §2 / Phase 7.5 — the design-doc gate fires only for the
    complex_large flow, and only when the architect hasn't already
    produced a design (re-entry idempotency).

    Phase 7.6 — the header check makes the gate immune to leftover
    artefacts from previous tasks that reused the same workspace path.
    A file with no header (legacy) or a header for a different task is
    treated as stale and reported as missing, so the gate falls through
    to ``run_design`` for a genuinely fresh task.
    """

    if not workspace_root:
        return False
    from agent.lifecycle.workspace_paths import format_design_header

    target = os.path.join(workspace_root, DESIGN_PATH)
    try:
        if not os.path.isfile(target):
            return False
        with open(target) as fh:
            head = fh.read(256)
    except OSError:  # pragma: no cover — defensive against odd FS errors
        return False
    expected = format_design_header(task_id)
    for line in head.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped == expected
    return False


async def _resolve_target_branch(parent_id: int) -> str:
    """Pick the integration PR target branch for the parent.

    Non-freeform parents always target ``main``. Freeform parents target
    the repo's ``FreeformConfig.dev_branch`` when one exists, falling
    back to ``main`` otherwise.
    """
    from shared.models import FreeformConfig

    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent_id))).scalar_one()
        if not p.freeform_mode or p.repo_id is None:
            return "main"
        cfg = (
            await s.execute(select(FreeformConfig).where(FreeformConfig.repo_id == p.repo_id))
        ).scalar_one_or_none()
        if cfg is None:
            return "main"
        return cfg.dev_branch or "main"


async def _open_integration_pr(parent: Task, target_branch: str) -> str:
    """Open the final ``<integration_branch> → target_branch`` PR via gh CLI.

    Phase 7.7 — three load-bearing properties:

    1. The workspace path is resolved (via ``_prepare_parent_workspace``,
       which is idempotent / clone-cached per task). Prior code didn't
       resolve it, so ``gh pr create`` ran from the orchestrator's cwd
       and died with "fatal: not a git repository".
    2. ``git push -u origin <branch>`` runs BEFORE ``gh pr create``, both
       with ``cwd=workspace``. Child PR merges only put the branch on
       the LOCAL working copy after the integration commit chain; the
       branch may not yet exist upstream, so the push is mandatory.
    3. On push or PR-create failure this function RAISES (RuntimeError).
       The caller — ``_open_integration_pr_and_transition`` — treats the
       exception as a signal to transition to BLOCKED instead of
       PR_CREATED, which is what production needs to see when the PR
       didn't actually open.

    The integration branch name comes from the resolver, which reads
    ``Task.integration_branch`` (new ``auto-agent/<slug>-<id>`` shape)
    or falls back to ``trio/<id>`` for tasks created before Phase 7.7.

    Returns the PR URL printed by ``gh pr create`` on stdout.
    """
    from agent import sh
    from agent.lifecycle.trio.architect import _prepare_parent_workspace
    from agent.lifecycle.trio.integration_branch import resolve_integration_branch
    from shared.github_auth import get_github_token

    integration_branch = resolve_integration_branch(parent)

    workspace = await _prepare_parent_workspace(parent)
    workspace_path = workspace.root if hasattr(workspace, "root") else str(workspace)

    try:
        token = await get_github_token(
            user_id=parent.created_by_user_id,
            organization_id=parent.organization_id,
        )
    except Exception as e:  # pragma: no cover — env-dependent
        log.warning(
            "trio.parent.gh_token_unavailable",
            parent_id=parent.id,
            error=str(e),
        )
        raise RuntimeError(f"gh token unavailable: {e}") from e

    gh_env = {"GH_TOKEN": token} if token else {}

    # 1. Push the integration branch upstream. Prior to Phase 7.7 this
    #    step was missing — the production run for task 1 lost the
    #    integration commits because nothing pushed them.
    push_res = await sh.run(
        ["git", "push", "-u", "origin", integration_branch],
        cwd=workspace_path,
        timeout=60,
        env=gh_env,
    )
    if push_res.failed:
        log.warning(
            "trio.parent.integration_branch_push_failed",
            parent_id=parent.id,
            branch=integration_branch,
            stderr=(push_res.stderr or "")[:500],
        )
        raise RuntimeError(
            f"git push {integration_branch} failed: {(push_res.stderr or '').strip()[:500]}"
        )

    # 2. Open the PR. Must also run with cwd=workspace so gh can locate
    #    the repository context (gh resolves the current branch + remote
    #    relative to cwd).
    title = f"trio: integration — {parent.title}"
    body = (
        f"Final integration PR for trio parent #{parent.id}.\n\n"
        "Contains every child PR that landed on the integration branch "
        f"`{integration_branch}` during the trio cycle."
    )
    create_res = await sh.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            target_branch,
            "--head",
            integration_branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=workspace_path,
        timeout=30,
        env=gh_env,
    )
    if create_res.failed:
        log.warning(
            "trio.parent.integration_pr_create_failed",
            parent_id=parent.id,
            branch=integration_branch,
            stderr=(create_res.stderr or "")[:500],
        )
        raise RuntimeError(
            f"gh pr create failed for {integration_branch}: "
            f"{(create_res.stderr or '').strip()[:500]}"
        )
    return (create_res.stdout or "").strip()


async def _block_parent(parent_id: int, message: str) -> None:
    """Transition parent to BLOCKED and clear trio_phase. Helper used by
    every terminal failure path in ``run_trio_parent``."""
    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent_id))).scalar_one()
        await transition(s, p, TaskStatus.BLOCKED, message=message)
        p.trio_phase = None
        await s.commit()


async def _mark_item_done(parent_id: int, item_id: str, head_sha: str | None) -> None:
    """Set an item's status to 'done' in tasks.trio_backlog. JSONB requires a
    fresh list reference for SQLAlchemy to mark the column dirty."""
    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent_id))).scalar_one()
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
    parent_id: int,
    old_item_id: str,
    new_items: list[dict],
) -> None:
    """Replace a single backlog item with one or more new items. Used by
    the architect's ``revise_backlog`` tiebreak decision."""
    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent_id))).scalar_one()
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
    """Make sure the workspace is on the parent's integration branch.

    The architect's initial pass may have left the workspace on the
    sibling init branch (``<integration_branch>-init``) after
    ``_commit_and_open_initial_pr``. The dispatcher operates on the
    integration branch, so we explicitly check it out before each item.

    Phase 7.7 — the branch name comes from the resolver so new tasks see
    ``auto-agent/<slug>-<id>`` and in-flight ones see the legacy
    ``trio/<id>``.
    """
    from agent import sh
    from agent.lifecycle.trio.integration_branch import resolve_integration_branch

    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent_id))).scalar_one()
        integration_branch = resolve_integration_branch(p)
    res = await sh.run(
        ["git", "checkout", integration_branch],
        cwd=workspace,
        timeout=30,
    )
    if res.failed:
        log.warning(
            "trio.parent.checkout_integration_failed",
            parent_id=parent_id,
            stderr=(res.stderr or "")[:300],
        )


async def _maybe_dispatch_sub_architects(parent: Task) -> bool:
    """If the architect emitted ``spawn_sub_architects``, dispatch and finish.

    ADR-015 §9 / Phase 8. Reads ``.auto-agent/decision.json``; on the
    spawn action transitions the parent to ``AWAITING_SUB_ARCHITECTS``,
    runs each slice through :mod:`agent.lifecycle.trio.sub_architect`
    serially, then transitions to ``FINAL_REVIEW`` (all complete) or
    ``BLOCKED`` (any slice failed).

    Returns ``True`` when the spawn path was taken (caller should not
    proceed into the per-item builder loop); ``False`` when no spawn
    decision is present.
    """

    from agent.lifecycle.trio import architect_decision, sub_architect
    from agent.lifecycle.trio.architect import _prepare_parent_workspace

    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
        # Detection requires a workspace path — re-use _prepare_parent_workspace
        # which is idempotent (clone is cached per task).
        workspace = await _prepare_parent_workspace(p)

    workspace_root = workspace.root if hasattr(workspace, "root") else str(workspace)

    decision = architect_decision.read_decision(workspace_root)
    if decision is None or decision.get("action") != "spawn_sub_architects":
        return False

    slices = (decision.get("payload") or {}).get("slices") or []
    if not slices:
        return False

    log.info(
        "trio.parent.spawn_sub_architects",
        parent_id=parent.id,
        slice_count=len(slices),
    )

    # Transition into AWAITING_SUB_ARCHITECTS to surface the pause on the UI.
    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
        try:
            await transition(
                s,
                p,
                TaskStatus.AWAITING_SUB_ARCHITECTS,
                message=f"trio: spawning {len(slices)} sub-architect slice(s)",
            )
        except Exception:
            p.status = TaskStatus.AWAITING_SUB_ARCHITECTS
        await s.commit()

    result = await sub_architect.dispatch_sub_architects(
        parent_task=p,
        workspace_root=workspace_root,
        slices=slices,
    )

    if not result.ok:
        await _block_parent(
            parent.id,
            f"trio: sub-architect dispatch failed — {result.blocked_reason}",
        )
        return True

    # All slices completed — same path as a flat backlog drain: transition
    # to FINAL_REVIEW. The final reviewer composes the design + slice
    # backlogs + integrated diff via the existing _drive_final_review_and_pr.
    target = await _resolve_target_branch(parent.id)
    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
        repo_name = p.repo.name if p.repo else None
        from agent.lifecycle.factory import home_dir_for_task

        home_dir = await home_dir_for_task(p)
        org_id = p.organization_id

    await _drive_final_review_and_pr(
        parent=parent,
        workspace_root=workspace_root,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
        target_branch=target,
    )
    return True


async def _advance_through_design_gate(parent: Task) -> bool:
    """ADR-015 §2 / Phase 7.5 — drive the complex_large design-doc gate.

    Returns ``True`` when the trio is clear to fall through to the
    per-item loop (the architect has produced a backlog, or the gate is
    not applicable). Returns ``False`` when the caller should exit
    without progress — either the gate has just been opened (waiting
    for an approval verdict to land) or the gate landed on a rejection
    that blocked the task.

    Decision tree:

    * Non-``complex_large`` parents (freeform-complex, freeform-simple)
      keep the legacy ``run_initial`` path — the design-doc gate is
      scoped to complex_large per ADR-015 §2.
    * Parents with a non-empty backlog already on disk: idempotent
      re-entry; skip the architect entirely so the dispatcher can
      resume the per-item loop where it was.
    * Parents in ``AWAITING_DESIGN_APPROVAL``: in freeform mode invoke
      the standin via :func:`run_freeform_gate` to auto-write
      ``plan_approval.json``, then read the verdict; in human-in-loop
      mode read the file the user wrote. Approval transitions to
      ARCHITECT_BACKLOG_EMIT and falls through to ``run_initial``;
      rejection transitions to BLOCKED. Missing file → return False.
    * Fresh parents with neither design.md nor backlog: invoke
      :func:`architect.run_design`. It writes the design and parks the
      task at AWAITING_DESIGN_APPROVAL. Return False — the orchestrator
      will re-enter via ``on_design_approved`` once the user (or
      standin) responds.
    """

    # Hoist parent state once — we need the live row to make every
    # decision below; ``parent`` may be stale after a fire-and-forget
    # re-entry from the approval-event handler.
    async with async_session() as _s:
        live = (await _s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
        complexity = live.complexity
        status = live.status
        has_backlog = bool(live.trio_backlog)

    # Non-complex_large parents keep the legacy single-pass flow. The
    # design gate is intentionally complex_large-only per §2 — freeform
    # complex/simple tasks routed through trio (run.py:1188) still emit
    # a backlog directly from ``run_initial``.
    if complexity != TaskComplexity.COMPLEX_LARGE:
        if not has_backlog:
            await _set_trio_phase(parent.id, TrioPhase.ARCHITECTING)
            await architect.run_initial(parent.id)
        else:
            log.info(
                "trio.parent.resume_skipping_run_initial",
                parent_id=parent.id,
            )
        return True

    # complex_large from here on. Prepare the workspace once — every
    # branch below reads or writes a file under it.
    from agent.lifecycle.trio.architect import _prepare_parent_workspace

    workspace = await _prepare_parent_workspace(live)
    workspace_root = workspace.root if hasattr(workspace, "root") else str(workspace)

    # Already has a backlog → idempotent re-entry; preserve dispatcher
    # progress and skip both run_design + run_initial.
    if has_backlog:
        log.info(
            "trio.parent.resume_skipping_run_initial",
            parent_id=parent.id,
        )
        return True

    # B) Parent is parked at the design-approval gate. Standin (freeform)
    #    or polling (human_in_loop) must produce plan_approval.json
    #    before we can move on.
    if status == TaskStatus.AWAITING_DESIGN_APPROVAL:
        await _try_freeform_design_standin(parent=live, workspace_root=workspace_root)
        try:
            advanced = await design_approval.resume_after_design_approval(
                task_id=parent.id,
                workspace=workspace_root,
            )
        except ValueError as exc:
            log.warning(
                "trio.parent.design_approval_malformed",
                parent_id=parent.id,
                error=str(exc),
            )
            return False
        if not advanced:
            # No verdict yet — orchestrator handler will re-enter.
            log.info(
                "trio.parent.design_gate_awaiting_verdict",
                parent_id=parent.id,
            )
            return False

        # The transition advanced us — re-read the live status to decide
        # what comes next. Approved → ARCHITECT_BACKLOG_EMIT (fall
        # through to run_initial below). Rejected → BLOCKED (caller
        # exits without progress).
        async with async_session() as _s:
            live2 = (await _s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
            status = live2.status

        if status == TaskStatus.BLOCKED:
            return False
        # else falls through to the backlog-emit branch below.

    # C) Backlog-emit step. Fires either after a fresh approval landed
    #    above OR if the orchestrator re-entered after the approval
    #    endpoint already transitioned to ARCHITECT_BACKLOG_EMIT.
    if status == TaskStatus.ARCHITECT_BACKLOG_EMIT or _design_md_exists(workspace_root, parent.id):
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECTING)
        await architect.run_initial(parent.id)
        return True

    # A) Fresh complex_large parent with no design.md and no backlog —
    #    run the design pass. ``architect.run_design`` writes
    #    ``.auto-agent/design.md`` and parks the task at
    #    ``AWAITING_DESIGN_APPROVAL`` via ``finalize_design``.
    #
    # The state machine requires TRIO_EXECUTING → ARCHITECT_DESIGNING →
    # AWAITING_DESIGN_APPROVAL — there's no direct edge from TRIO_EXECUTING
    # to AWAITING_DESIGN_APPROVAL. Move the wire status here so the
    # finalize_design call below validates.
    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
        await transition(
            s,
            p,
            TaskStatus.ARCHITECT_DESIGNING,
            message="trio: architect designing",
        )
        await s.commit()
    await _set_trio_phase(parent.id, TrioPhase.ARCHITECTING)
    await architect.run_design(parent.id)

    # ``run_design`` just transitioned the task to AWAITING_DESIGN_APPROVAL.
    # Re-enter the helper so case B (the standin / verdict path) fires on
    # the SAME invocation. Without this, freeform-mode children deadlock:
    # nothing else re-invokes the trio dispatcher (the trio recovery hook
    # only picks TRIO_EXECUTING, and on_design_approved only fires after a
    # verdict that the standin would have written). Recursion depth is
    # bounded — case B either approves+returns True, rejects+returns False,
    # or fails the verdict file lookup+returns False; none recurse back.
    async with async_session() as _s:
        live_after_design = (
            await _s.execute(select(Task).where(Task.id == parent.id))
        ).scalar_one()
    if live_after_design.status == TaskStatus.AWAITING_DESIGN_APPROVAL:
        return await _advance_through_design_gate(live_after_design)
    return False


async def _try_freeform_design_standin(
    *,
    parent: Task,
    workspace_root: str,
) -> None:
    """In freeform mode, dispatch the standin at the design gate.

    Reads ``.auto-agent/design.md`` and hands it to
    :func:`run_freeform_gate` with ``gate="design_approval"``. The
    standin writes ``plan_approval.json`` (canonical gate file) which
    :func:`design_approval.resume_after_design_approval` picks up on
    the same call. Best-effort: any error here is logged but doesn't
    break the gate — the resume function will report ``False`` and the
    flow waits for a human verdict.
    """

    repo = getattr(parent, "repo", None)
    if repo is None and getattr(parent, "repo_id", None) is not None:
        # The ``parent`` may have been loaded outside an active session
        # (e.g. by the trio recovery hook on startup) so the lazy ``repo``
        # relationship is unresolved. Fetch the row directly by
        # ``repo_id``. Without this fallback the standin returns silently
        # and every recovered freeform child deadlocks at the design gate.
        from shared.models import Repo

        async with async_session() as _s:
            repo = (
                await _s.execute(select(Repo).where(Repo.id == parent.repo_id))
            ).scalar_one_or_none()

    if repo is None:
        log.warning(
            "trio.parent.freeform_design_standin_no_repo",
            parent_id=parent.id,
            repo_id=getattr(parent, "repo_id", None),
        )
        return

    design_path = os.path.join(workspace_root, DESIGN_PATH)
    try:
        with open(design_path) as fh:
            design_md = fh.read()
    except OSError:
        design_md = ""

    # Phase 7.6 — strip the task-id header before handing the markdown to
    # the standin. The standin shouldn't have to know about the header
    # convention; it just needs the design content.
    from agent.lifecycle.workspace_paths import strip_design_header

    design_md = strip_design_header(design_md)

    try:
        fired = await run_freeform_gate(
            task=parent,
            repo=repo,
            gate="design_approval",
            gate_input={"design_md": design_md},
            context={"workspace_root": workspace_root},
        )
    except Exception as exc:  # pragma: no cover — defensive
        log.warning(
            "trio.parent.freeform_design_standin_failed",
            parent_id=parent.id,
            error=str(exc),
        )
        return

    if fired:
        log.info(
            "trio.parent.freeform_design_standin_fired",
            parent_id=parent.id,
        )


async def run_trio_parent(
    parent: Task,
    *,
    repair_context: dict | None = None,
    iteration_context: dict | None = None,
) -> None:
    """Drive a parent task through the trio cycle, open the final PR.

    Fresh entry (both context kwargs ``None``) runs the architect's
    initial pass. Re-entry from a failed integration PR threads the CI
    log into a checkpoint pass (``repair_context``) so the architect can
    add fix work items. Re-entry from user iteration feedback (``iteration_context``)
    routes to ``architect.iterate`` and skips final-PR creation (the PR
    already exists — the per-item loop pushes new commits to it).

    Per ADR-013 the per-item loop no longer creates child Task rows. It
    invokes :mod:`agent.lifecycle.trio.dispatcher`, which runs coder
    and reviewer subagents inside the parent's slot. The dispatcher
    returns an ``ItemResult``; this function persists backlog updates
    and acts on architect tiebreak decisions.

    ADR-015 §2 / Phase 7.5 — for ``complex_large`` parents the architect
    runs in two turns separated by a human (or standin) approval gate:
    ``architect.run_design`` produces ``.auto-agent/design.md`` and
    parks the task at ``AWAITING_DESIGN_APPROVAL``; on approval the
    task re-enters here (via :func:`on_design_approved` in ``run.py``)
    in state ``ARCHITECT_BACKLOG_EMIT`` and ``architect.run_initial``
    emits the backlog with the design doc pinned in context.
    """
    from agent.lifecycle.trio.architect import _prepare_parent_workspace

    if iteration_context is not None:
        # ADR-017 — user sent feedback on an existing PR.  The architect
        # appends new pending backlog items; the per-item loop below picks
        # them up naturally.  We do NOT open a second integration PR at the
        # tail — the existing PR already covers the branch; we just push new
        # commits to it and transition back to AWAITING_REVIEW.
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECT_ITERATING)
        await architect.iterate(parent.id, iteration_context=iteration_context)
    elif repair_context is not None:
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECT_CHECKPOINT)
        await architect.checkpoint(parent.id, repair_context=repair_context)
    else:
        # Phase 7.5 — design-gate-aware front half. Reads the live task
        # state + on-disk artefacts and decides whether to run the design
        # pass, resume after approval, or fall through to run_initial.
        # Returns False when the gate is still open (task is waiting on
        # an approval file) so the caller can return without progress.
        gate_ok = await _advance_through_design_gate(parent)
        if not gate_ok:
            return

    # ADR-015 §9 / Phase 8 — if the architect emitted spawn_sub_architects
    # the per-item builder loop does not apply; the sub-architect dispatcher
    # takes over. Detection lives here so a freshly-emitted decision.json
    # routes correctly without bouncing through the per-item loop first.
    if await _maybe_dispatch_sub_architects(parent):
        return

    # Resolve once per cycle — re-cloning per item is wasteful and the
    # subagents share the workspace.
    parent_workspace: str | None = None
    repo_name: str | None = None
    home_dir: str | None = None
    org_id: int | None = None
    parent_repo_id: int | None = None

    while True:
        # Re-read the backlog each iteration in case the architect or a
        # tiebreak revised it.
        async with async_session() as s:
            p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
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
                parent_repo_id = p.repo_id

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
            repo_id=parent_repo_id,
        )

        if result.ok:
            await _mark_item_done(parent.id, item_id, result.head_sha)
            log.info(
                "trio.parent.item_done",
                parent_id=parent.id,
                item_id=item_id,
                head_sha=result.head_sha,
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
            parent_id=parent.id,
            item_id=item_id,
            action=action,
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
                p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
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
            question = str(decision.get("question") or "Trio is stuck; please advise.")
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

    # ADR-017 — iteration tail: the per-item loop pushed new commits onto
    # the existing integration branch.  The PR is already open; opening a
    # second one would duplicate it.  Transition ITERATING → AWAITING_REVIEW
    # and publish task_iteration_complete so downstream listeners (UI, Slack)
    # know the PR has been updated.
    if iteration_context is not None:
        async with async_session() as s:
            p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
            await transition(
                s,
                p,
                TaskStatus.AWAITING_REVIEW,
                message="trio: iteration complete — PR updated with new commits",
            )
            await s.commit()
        await publish(
            task_iteration_complete(
                parent.id,
                summary="updated PR with your changes",
            )
        )
        return

    # ADR-015 §4 / Phase 7 — backlog drained → final reviewer runs over
    # the integrated diff. On gaps_found the architect's persisted
    # session resumes (gap_fix) and dispatches new backlog items; we
    # loop back into the per-item phase. Bounded at 3 gap-fix rounds.
    # On passed → PR creation path (existing).
    if parent_workspace is None:
        # Defensive — should not happen if a backlog was emitted, but
        # rebuild the workspace context so final_review can read the
        # design/reviews/diff.
        async with async_session() as s:
            p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
            from agent.lifecycle.factory import home_dir_for_task

            parent_workspace = await _prepare_parent_workspace(p)
            repo_name = p.repo.name if p.repo else None
            home_dir = await home_dir_for_task(p)
            org_id = p.organization_id

    workspace_root = (
        parent_workspace.root if hasattr(parent_workspace, "root") else str(parent_workspace)
    )

    target = await _resolve_target_branch(parent.id)

    await _drive_final_review_and_pr(
        parent=parent,
        workspace_root=workspace_root,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
        target_branch=target,
    )


async def _drive_final_review_and_pr(
    *,
    parent: Task,
    workspace_root: str,
    repo_name: str | None,
    home_dir: str | None,
    org_id: int | None,
    target_branch: str,
) -> None:
    """ADR-015 §4 / Phase 7 — final review → optional gap-fix loop → PR.

    Bounded at 3 gap-fix rounds. After 3 rounds with gaps still present
    the parent is BLOCKED. On a passed verdict we open the integration
    PR and transition to PR_CREATED — the PR-reviewer (Phase 5) runs
    from there per the complex_large branch in ``coding._open_pr_and_advance``.
    """

    from agent.lifecycle.trio import final_reviewer, gap_fix

    previous_gaps: list[dict] | None = None
    previous_attempt_summary = ""

    for round_idx in range(1, gap_fix.MAX_GAP_FIX_ROUNDS + 2):  # +1 for the bound check
        # Park the task in FINAL_REVIEW for visibility.
        async with async_session() as s:
            p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
            if p.status != TaskStatus.FINAL_REVIEW:
                try:
                    await transition(
                        s,
                        p,
                        TaskStatus.FINAL_REVIEW,
                        message=f"trio: final review round {round_idx}",
                    )
                except Exception:
                    p.status = TaskStatus.FINAL_REVIEW
                await s.commit()

        review = await final_reviewer.run_final_review(
            workspace_root=workspace_root,
            parent_task_id=parent.id,
            base_branch=target_branch,
            previous_gaps=previous_gaps,
            previous_attempt_summary=previous_attempt_summary,
            repo_name=repo_name,
            home_dir=home_dir,
            org_id=org_id,
            repo_id=parent.repo_id,
        )

        if review.verdict == "passed":
            await _open_integration_pr_and_transition(parent=parent, target_branch=target_branch)
            return

        # gaps_found → architect gap-fix (bounded).
        if round_idx > gap_fix.MAX_GAP_FIX_ROUNDS:
            await _block_parent(
                parent.id,
                f"trio: final review still finds gaps after "
                f"{gap_fix.MAX_GAP_FIX_ROUNDS} gap-fix rounds",
            )
            return

        async with async_session() as s:
            p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
            try:
                await transition(
                    s,
                    p,
                    TaskStatus.ARCHITECT_GAP_FIX,
                    message=(
                        f"trio: gap-fix round {round_idx} — {len(review.gaps)} gap(s) to close"
                    ),
                )
            except Exception:
                p.status = TaskStatus.ARCHITECT_GAP_FIX
            await s.commit()

        decision = await gap_fix.run_gap_fix(
            parent_task_id=parent.id,
            gaps=review.gaps,
            round_idx=round_idx,
        )
        action = decision.get("action")
        if action == "dispatch_new":
            new_items = decision.get("items") or []
            await _append_backlog_items(parent.id, new_items)
            # Hop back into the per-item builder loop; the outer
            # ``run_trio_parent`` recurse below picks the pending items
            # up.
            async with async_session() as s:
                p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
                try:
                    await transition(
                        s,
                        p,
                        TaskStatus.TRIO_EXECUTING,
                        message="trio: gap-fix dispatched new items",
                    )
                except Exception:
                    p.status = TaskStatus.TRIO_EXECUTING
                await s.commit()
            previous_gaps = list(review.gaps)
            previous_attempt_summary = f"round {round_idx}: dispatched {len(new_items)} new items"
            await run_trio_parent(parent)
            return
        # architect escalated, blocked, or unknown action
        await _block_parent(
            parent.id,
            f"trio: gap-fix architect emitted action={action!r} — "
            f"{decision.get('reason', '')[:200]}",
        )
        return


async def _append_backlog_items(parent_id: int, new_items: list[dict]) -> None:
    """Append architect-emitted new items to the parent's trio_backlog.

    Items default to ``status="pending"`` so the dispatcher picks them
    up on the next per-item loop.
    """

    if not new_items:
        return
    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent_id))).scalar_one()
        backlog = list(p.trio_backlog or [])
        for item in new_items:
            ni = dict(item)
            ni.setdefault("status", "pending")
            backlog.append(ni)
        p.trio_backlog = backlog
        await s.commit()


async def _open_integration_pr_and_transition(
    *,
    parent: Task,
    target_branch: str,
) -> None:
    """Open the integration PR + transition the parent to PR_CREATED.

    Phase 7.7 — when ``_open_integration_pr`` raises (push failed or
    ``gh pr create`` failed), the parent transitions to BLOCKED with the
    failure reason. Prior code swallowed failure inside
    ``_open_integration_pr`` and still transitioned to PR_CREATED with an
    empty ``pr_url``, hiding the breakage.
    """

    async with async_session() as s:
        p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
        try:
            pr_url = await _open_integration_pr(p, target_branch)
        except Exception as e:
            log.warning(
                "trio.parent.integration_pr_failed",
                parent_id=parent.id,
                error=str(e),
            )
            p.trio_phase = None
            await transition(
                s,
                p,
                TaskStatus.BLOCKED,
                message=f"trio: integration PR failed — {str(e)[:300]}",
            )
            await s.commit()
            return

        p.pr_url = pr_url or None
        p.trio_phase = None
        await transition(s, p, TaskStatus.PR_CREATED, message="trio: integration PR opened")
        # ADR-017 — PR_CREATED is a single-fire transit event; AWAITING_REVIEW
        # is the long-lived state. Fall through immediately so the task lands
        # in the right phase for the iteration loop to engage.
        await transition(s, p, TaskStatus.AWAITING_REVIEW, message="trio: awaiting review/feedback")
        await s.commit()
        branch = p.integration_branch or ""

    await publish(
        task_pr_created(parent.id, pr_url=pr_url or "", branch=branch),
    )

    log.info(
        "trio.parent.opened_final_pr",
        parent_id=parent.id,
        pr_url=pr_url,
        target_branch=target_branch,
    )
