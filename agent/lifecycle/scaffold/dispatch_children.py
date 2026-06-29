"""Phase D — dispatch per-domain child trios — ADR-018 §7.

For each approved domain ADR, create a child ``Task`` with
``complexity=COMPLEX_LARGE`` and ``parent_task_id=<scaffold parent>``.
The child's ``.auto-agent/design.md`` is seeded with the domain ADR's
markdown so the existing trio architect treats the ADR as its design.

Children open separate PRs (no shared integration branch) and respect
the existing 2-slot concurrency pool via the standard task_created
event pipeline.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from sqlalchemy import select

from agent.lifecycle.scaffold._verdicts import read_all_verdicts as _read_all_verdicts
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.scaffold.validators import parse_domains
from agent.lifecycle.workspace_paths import (
    ROOT_ADR_PATH,
    domain_adr_path,
)
from shared.database import async_session
from shared.events import publish, task_created
from shared.models import Task, TaskSource, TaskStatus
from shared.repo_secrets import list_missing_architect_required

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# ADR-019 T7 — Phase C-to-D secrets gate helpers
# ---------------------------------------------------------------------------


async def _transition_parent(task_id: int, to_status: TaskStatus) -> bool:
    """Transition the scaffold parent to a new status using a fresh session.

    Returns True if the transition fired, False if it was skipped because the
    task was already at or past ``to_status`` (concurrent PUT-hook call).

    Catches InvalidTransition to make concurrent PUT-hook calls safe — if
    another request already advanced the task past this status, the second
    caller silently returns False instead of bubbling a 500.
    """
    from orchestrator.state_machine import InvalidTransition, transition

    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        try:
            await transition(
                s,
                task,
                to_status,
                message=(
                    "All required secrets populated — proceeding to Phase D"
                    if to_status == TaskStatus.DISPATCHING_DOMAIN_BUILDS
                    else f"Transitioning to {to_status.value}"
                ),
            )
            await s.commit()
            return True
        except InvalidTransition as e:
            log.warning(
                "scaffold.transition_skipped",
                task_id=task_id,
                target=to_status.value,
                reason=str(e),
            )
            return False


async def _persist_missing_secrets(task_id: int, missing: list[str]) -> None:
    """Persist the list of missing secrets onto task.subtasks for UI display."""
    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        bucket = task.subtasks if isinstance(task.subtasks, dict) else {}
        new_bucket = dict(bucket)
        scaffold = new_bucket.get("scaffold") or {}
        if not isinstance(scaffold, dict):
            scaffold = {}
        new_scaffold = dict(scaffold)
        new_scaffold["missing_secrets"] = missing
        new_bucket["scaffold"] = new_scaffold
        task.subtasks = new_bucket
        await s.commit()


async def check_secrets_gate(task: Task) -> bool:
    """Check whether all architect-required secrets are populated.

    ADR-019 T7 — called when a SCAFFOLD parent enters
    ``AWAITING_REQUIRED_SECRETS``. Returns True if the gate is green
    (all secrets set, transition fired to DISPATCHING_DOMAIN_BUILDS),
    False if the parent is parked and still waiting.
    """
    missing = await list_missing_architect_required(
        task.repo_id,
        organization_id=task.organization_id,
    )

    if missing:
        log.info(
            "scaffold.secrets_gate.parked",
            task_id=task.id,
            missing=missing,
        )
        await _persist_missing_secrets(task.id, missing)
        return False

    log.info("scaffold.secrets_gate.green", task_id=task.id)
    fired = await _transition_parent(task.id, TaskStatus.DISPATCHING_DOMAIN_BUILDS)
    # Return True only when the transition actually fired so the caller
    # (PUT hook / recheck endpoint) knows whether to invoke run_scaffold_parent.
    # If fired=False a concurrent request already advanced the task; the
    # other caller owns the dispatch.
    return fired


async def run(task: Task) -> list[int]:
    """Spawn one child Task per approved domain. Returns the child IDs.

    Idempotent on re-entry: if a child for a given domain slug already
    exists (matched on ``Task.title`` containing the slug under this
    parent_task_id), it is skipped. v1 only — Stage 4 will tighten this
    by recording the slug somewhere stable.
    """

    workspace = await prepare_scaffold_workspace(task)

    root_adr_path = os.path.join(workspace, ROOT_ADR_PATH)
    if not os.path.isfile(root_adr_path):
        log.warning(
            "scaffold.dispatch.root_adr_missing",
            task_id=task.id,
            path=root_adr_path,
        )
        return []
    with open(root_adr_path) as fh:
        root_adr_md = fh.read()

    domains = parse_domains(root_adr_md)
    verdicts = _read_all_verdicts(workspace)

    created_ids: list[int] = []

    async with async_session() as s:
        # Pull existing children once so re-entry is idempotent on titles.
        existing_children = (
            (await s.execute(select(Task).where(Task.parent_task_id == task.id))).scalars().all()
        )
        existing_titles = {(c.title or "") for c in existing_children}

        for idx, domain in enumerate(domains, start=1):
            slug = domain.get("slug") or ""
            if not slug:
                continue
            verdict = (verdicts.get(slug) or {}).get("verdict")
            if verdict != "approved":
                continue

            name = domain.get("name") or slug
            adr_rel = domain_adr_path(idx, slug)
            adr_abs = os.path.join(workspace, adr_rel)
            adr_md = ""
            if os.path.isfile(adr_abs):
                try:
                    with open(adr_abs) as fh:
                        adr_md = fh.read()
                except OSError:
                    log.warning(
                        "scaffold.dispatch.adr_read_failed",
                        task_id=task.id,
                        slug=slug,
                        path=adr_abs,
                    )

            child_title = f"Domain build: {name} ({slug})"
            if child_title in existing_titles:
                log.info(
                    "scaffold.dispatch.skip_existing_child",
                    task_id=task.id,
                    slug=slug,
                )
                continue

            child_description = (
                f"Domain build child of scaffold parent #{task.id}.\n\n"
                f"Domain: {name} (slug `{slug}`)\n\n"
                f"Build this domain per its ADR. The ADR was copied into "
                f"`.auto-agent/design.md` so the trio architect treats it "
                f"as the design.\n\n"
                f"## Domain ADR\n\n{adr_md}"
            )

            child = Task(
                title=child_title,
                description=child_description,
                source=task.source or TaskSource.MANUAL,
                status=TaskStatus.INTAKE,
                complexity=task.complexity.__class__.COMPLEX_LARGE
                if task.complexity is not None
                else None,
                # Fallback when complexity is somehow None on the parent.
                repo_id=task.repo_id,
                freeform_mode=bool(task.freeform_mode),
                parent_task_id=task.id,
                organization_id=task.organization_id,
                created_by_user_id=task.created_by_user_id,
            )
            # Belt-and-suspenders: ensure complexity is set even if the
            # parent has somehow lost it (e.g. older fixture). Importing
            # the enum lazily keeps the file's import surface tight.
            if child.complexity is None:
                from shared.models import TaskComplexity

                child.complexity = TaskComplexity.COMPLEX_LARGE

            s.add(child)
            await s.flush()
            created_ids.append(child.id)

            log.info(
                "scaffold.dispatch.child_created",
                task_id=task.id,
                child_id=child.id,
                slug=slug,
            )

        await s.commit()

    # Seed each child's design.md after commit. We do this here (not
    # before commit) so the child id is stable. v1: write to a path
    # under the parent's workspace using a sub-dir per child id; the
    # trio architect's ``_prepare_parent_workspace`` will fetch its own
    # workspace and the orchestrator-stage's Phase D wiring (Stage 4)
    # will handle the cross-workspace copy properly. For now we just
    # ensure the file exists at the parent-workspace mirror.
    for child_id, idx_slug in zip(created_ids, _approved_slugs(domains, verdicts), strict=False):
        _idx, slug = idx_slug
        adr_rel = domain_adr_path(_idx, slug)
        adr_abs = os.path.join(workspace, adr_rel)
        if os.path.isfile(adr_abs):
            # Mirror at .auto-agent/children/<child_id>/design.md for
            # debugging/observability; the real copy happens when the
            # child's workspace is prepared by the trio architect (it
            # writes its own design.md via the architect's design pass,
            # but Stage 4 will short-circuit that with the ADR as input).
            mirror = os.path.join(
                workspace,
                ".auto-agent",
                "children",
                str(child_id),
                "design.md",
            )
            os.makedirs(os.path.dirname(mirror), exist_ok=True)
            try:
                with open(adr_abs) as src, open(mirror, "w") as dst:
                    dst.write(src.read())
            except OSError:
                pass

    # Bug 17 — child trios under a scaffold parent must build serially.
    # Coding in parallel produces concurrent commits on per-task branches of
    # the same repo and is, per the user's contract, "not correct
    # functionality". Publishing ``task_created`` for every child here would
    # race them through ``on_task_classified`` → TRIO_EXECUTING simultaneously
    # because the per-repo concurrency cap doesn't count TRIO_EXECUTING as
    # active. Instead we only kick the FIRST pending child; the fan-in handler
    # (``_maybe_advance_scaffold_parent_on_child_finish`` in run.py) dispatches
    # the next pending sibling on each terminal event. Re-entry safety: we
    # look across ALL children (existing INTAKE + newly created) so a
    # re-dispatch after a partial run still picks the right next one.
    await _publish_next_scaffold_child(task.id)

    return created_ids


async def _publish_next_scaffold_child(parent_id: int) -> int | None:
    """Publish ``task_created`` for the next pending scaffold child if any.

    Picks the lowest-id ``INTAKE`` child of ``parent_id`` and publishes a
    ``task_created`` event for it. Skips if any sibling is already in a
    non-terminal, non-INTAKE status (i.e. a build is already in flight) so
    we don't accidentally fan out by re-firing during recovery.

    Returns the child id we dispatched, or ``None`` if nothing was eligible.
    """

    # Local copy of the terminal statuses; importing from run.py would
    # create a cycle (run.py imports from this module).
    terminal = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BLOCKED)

    async with async_session() as s:
        children = (
            (await s.execute(select(Task).where(Task.parent_task_id == parent_id)))
            .scalars()
            .all()
        )

    if not children:
        return None

    # If any child is mid-flight (not INTAKE, not terminal), do nothing —
    # we serialize one-at-a-time.
    in_flight = [
        c for c in children if c.status != TaskStatus.INTAKE and c.status not in terminal
    ]
    if in_flight:
        log.info(
            "scaffold.dispatch.serial_in_flight",
            parent_id=parent_id,
            in_flight_ids=[c.id for c in in_flight],
        )
        return None

    intake = sorted(
        [c for c in children if c.status == TaskStatus.INTAKE], key=lambda c: c.id
    )
    if not intake:
        return None

    next_child = intake[0]
    log.info(
        "scaffold.dispatch.publish_next_child",
        parent_id=parent_id,
        child_id=next_child.id,
        remaining_intake=len(intake) - 1,
    )
    await publish(task_created(next_child.id))
    return next_child.id


def _approved_slugs(
    domains: list[dict], verdicts: dict[str, dict[str, Any]]
) -> list[tuple[int, str]]:
    """Return ``(index, slug)`` pairs for every approved domain, in order."""

    out: list[tuple[int, str]] = []
    for idx, d in enumerate(domains, start=1):
        slug = d.get("slug") or ""
        if not slug:
            continue
        if (verdicts.get(slug) or {}).get("verdict") == "approved":
            out.append((idx, slug))
    return out
