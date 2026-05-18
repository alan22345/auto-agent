"""Phase B-gate — root ADR approval — ADR-018 §4.

The orchestrator's HTTP layer (Stage 4 in ADR-018) POSTs a verdict
payload here when a user (or freeform PO standin) reviews the root ADR.
This module owns the transition rules:

- ``approved`` → BUILDING_DOMAIN_ADRS, then re-invokes the scaffold
  driver so Phase C starts.
- ``revise``  → BUILDING_ROOT_ADR (architect resumes); bounded at
  3 rounds, after which we BLOCK with a "revise loop exhausted" message.
- ``rejected`` → BLOCKED.

Revise counter lives in ``Task.subtasks`` (a JSONB column already on
the Task model). We store a small dict ``{"scaffold": {"root_revise":
N}}`` under a stable key so we don't collide with any other JSONB
consumer.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from sqlalchemy import select

from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from orchestrator.state_machine import transition
from shared.database import async_session
from shared.models import Task, TaskStatus

log = structlog.get_logger()


MAX_ROOT_REVISE_ROUNDS = 3


# ---------------------------------------------------------------------------
# Counter helpers — backed by Task.subtasks JSONB.
# ---------------------------------------------------------------------------


_SCAFFOLD_KEY = "scaffold"
_ROOT_REVISE_KEY = "root_revise"


def _get_root_revise_count(task: Task) -> int:
    bucket = (task.subtasks or {}) if isinstance(task.subtasks, dict) else {}
    scaffold = bucket.get(_SCAFFOLD_KEY) if isinstance(bucket, dict) else None
    if not isinstance(scaffold, dict):
        return 0
    raw = scaffold.get(_ROOT_REVISE_KEY)
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _bump_root_revise_count(task: Task) -> int:
    """Increment the revise counter on ``task`` (in-memory) and return new value.

    Caller is responsible for committing the surrounding session.
    """

    current = _get_root_revise_count(task)
    new_value = current + 1
    bucket = task.subtasks if isinstance(task.subtasks, dict) else {}
    # Always rebuild the dict — JSONB needs a fresh reference for SQLAlchemy
    # to detect the change.
    new_bucket = dict(bucket)
    scaffold = new_bucket.get(_SCAFFOLD_KEY) or {}
    if not isinstance(scaffold, dict):
        scaffold = {}
    new_scaffold = dict(scaffold)
    new_scaffold[_ROOT_REVISE_KEY] = new_value
    new_bucket[_SCAFFOLD_KEY] = new_scaffold
    task.subtasks = new_bucket
    return new_value


# ---------------------------------------------------------------------------
# Verdict application
# ---------------------------------------------------------------------------


_VALID_VERDICTS = {"approved", "revise", "rejected"}


async def apply_verdict(task_id: int, verdict_payload: dict[str, Any]) -> TaskStatus:
    """Apply a root-ADR verdict to ``task_id``.

    ``verdict_payload`` shape:
        {"verdict": "approved" | "revise" | "rejected", "comments": str}

    Returns the resulting ``TaskStatus``. Caller (router) is responsible
    for re-invoking ``run_scaffold_parent`` after a successful approved
    or revise transition so the driver picks up from the new state.

    Raises ``ValueError`` for an unknown verdict.
    """

    verdict = (verdict_payload or {}).get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"root ADR verdict must be one of {sorted(_VALID_VERDICTS)}; got {verdict!r}"
        )
    comments = str((verdict_payload or {}).get("comments") or "").strip()

    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()

        if verdict == "approved":
            await transition(
                s,
                task,
                TaskStatus.BUILDING_DOMAIN_ADRS,
                message=("Root ADR approved" + (f": {comments[:200]}" if comments else "")),
            )
            await s.commit()
            log.info("scaffold.root_adr.approved", task_id=task_id)
            return TaskStatus.BUILDING_DOMAIN_ADRS

        if verdict == "rejected":
            await transition(
                s,
                task,
                TaskStatus.BLOCKED,
                message=("Root ADR rejected: " + (comments[:500] or "(no comments provided)")),
            )
            await s.commit()
            log.info("scaffold.root_adr.rejected", task_id=task_id)
            return TaskStatus.BLOCKED

        # verdict == "revise"
        new_count = _bump_root_revise_count(task)
        if new_count > MAX_ROOT_REVISE_ROUNDS:
            await transition(
                s,
                task,
                TaskStatus.BLOCKED,
                message=(
                    f"Root ADR revise loop exhausted ({new_count - 1} rounds): "
                    + (comments[:300] or "(no comments)")
                ),
            )
            await s.commit()
            log.warning(
                "scaffold.root_adr.revise_exhausted",
                task_id=task_id,
                rounds=new_count - 1,
            )
            return TaskStatus.BLOCKED

        await transition(
            s,
            task,
            TaskStatus.BUILDING_ROOT_ADR,
            message=(
                f"Root ADR revise round {new_count}" + (f": {comments[:200]}" if comments else "")
            ),
        )
        await s.commit()
        log.info(
            "scaffold.root_adr.revise",
            task_id=task_id,
            round=new_count,
        )
        return TaskStatus.BUILDING_ROOT_ADR


async def request_po_verdict(task: Task) -> str:
    """Ask the PO standin to verdict the root ADR (freeform mode).

    Reads ``.auto-agent/adrs/000-system.md`` from the scaffold workspace,
    delegates to ``agent.po_agent.po_approve_root_adr``, and returns the
    absolute path of the verdict file. The caller (parent driver) then
    invokes :func:`apply_verdict` with the verdict payload that
    ``po_approve_root_adr`` wrote.

    Caller is responsible for checking ``task.freeform_mode is True``
    before invoking this — the human-in-loop path waits for the user to
    POST a verdict via the router instead.
    """

    from agent.lifecycle.workspace_paths import ROOT_ADR_APPROVAL_PATH, ROOT_ADR_PATH
    from agent.po_agent import po_approve_root_adr

    workspace = await prepare_scaffold_workspace(task)

    adr_md = ""
    adr_abs = os.path.join(workspace, ROOT_ADR_PATH)
    if os.path.isfile(adr_abs):
        try:
            with open(adr_abs) as fh:
                adr_md = fh.read()
        except OSError:
            adr_md = ""

    await po_approve_root_adr(task, adr_md, workspace)

    verdict_abs = os.path.join(workspace, ROOT_ADR_APPROVAL_PATH)
    log.info(
        "scaffold.root_adr.po_standin_verdict_written",
        task_id=task.id,
        path=verdict_abs,
    )
    return verdict_abs


__all__ = [
    "MAX_ROOT_REVISE_ROUNDS",
    "apply_verdict",
    "request_po_verdict",
]
