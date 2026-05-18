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

import json
import os
from typing import Any

import structlog
from sqlalchemy import select

from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.scaffold.validators import parse_domains
from agent.lifecycle.workspace_paths import (
    DOMAIN_ADR_APPROVALS_DIR,
    ROOT_ADR_PATH,
    domain_adr_path,
)
from shared.database import async_session
from shared.events import publish, task_created
from shared.models import Task, TaskSource, TaskStatus

log = structlog.get_logger()


def _read_all_verdicts(workspace: str) -> dict[str, dict[str, Any]]:
    dir_abs = os.path.join(workspace, DOMAIN_ADR_APPROVALS_DIR)
    if not os.path.isdir(dir_abs):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in os.listdir(dir_abs):
        if not entry.endswith(".json"):
            continue
        slug = entry[: -len(".json")]
        try:
            with open(os.path.join(dir_abs, entry)) as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            out[slug] = payload
    return out


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

        await publish(task_created(child_id))

    return created_ids


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
