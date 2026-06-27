"""Phase C-gate — per-domain ADR approvals — ADR-018 §6.

Each domain ADR has its own verdict file under
``.auto-agent/domain_adr_approvals/<slug>.json``. The web-next pane
posts to a router endpoint (Stage 4); that endpoint calls
:func:`apply_verdict` per row.

Transition rules:
- ``approved`` / ``rejected`` are terminal for that domain — its verdict
  is recorded under the slug.
- ``revise`` keeps the domain open; the orchestrator re-runs the
  matching domain architect's session via a re-entry into Phase C.
- The parent transitions ``AWAITING_DOMAIN_ADR_APPROVAL →
  DISPATCHING_DOMAIN_BUILDS`` only when **every** domain has a
  non-``revise`` verdict and at least one is ``approved``.
- The revise counter is bounded per-domain at 3 rounds; the 4th
  ``revise`` becomes ``rejected`` automatically.
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
from sqlalchemy import select

from agent.lifecycle.scaffold._promotion import promote_adr_to_docs
from agent.lifecycle.scaffold._verdicts import read_all_verdicts as _read_all_verdicts
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.scaffold.validators import parse_domains
from agent.lifecycle.workspace_paths import (
    ROOT_ADR_PATH,
    domain_adr_approval_path,
    domain_adr_path,
    domain_grill_path,
)
from orchestrator.state_machine import transition
from shared.database import async_session
from shared.models import Task, TaskStatus

log = structlog.get_logger()


MAX_DOMAIN_REVISE_ROUNDS = 3


_VALID_VERDICTS = {"approved", "revise", "rejected"}


def _write_verdict(workspace: str, slug: str, payload: dict[str, Any]) -> str:
    """Persist a verdict JSON file for ``slug``. Returns the absolute path."""

    rel = domain_adr_approval_path(slug)
    abs_path = os.path.join(workspace, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as fh:
        json.dump(payload, fh, indent=2)
    return abs_path


async def apply_verdict(
    task_id: int,
    slug: str,
    verdict_payload: dict[str, Any],
) -> TaskStatus:
    """Apply one domain ADR verdict, return the parent's resulting status.

    Caller (router) re-invokes ``run_scaffold_parent`` after this
    returns so the driver picks up from the new state.
    """

    verdict = (verdict_payload or {}).get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"domain ADR verdict must be one of {sorted(_VALID_VERDICTS)}; got {verdict!r}"
        )
    comments = str((verdict_payload or {}).get("comments") or "").strip()

    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        workspace = await prepare_scaffold_workspace(task)

        existing = _read_all_verdicts(workspace).get(slug, {})
        revise_count = int(existing.get("revise_count") or 0)

        effective = verdict
        if verdict == "revise":
            revise_count += 1
            if revise_count > MAX_DOMAIN_REVISE_ROUNDS:
                effective = "rejected"
                comments = f"revise loop exhausted ({MAX_DOMAIN_REVISE_ROUNDS} rounds)" + (
                    f": {comments[:200]}" if comments else ""
                )

        payload = {
            "schema_version": "1",
            "slug": slug,
            "verdict": effective,
            "comments": comments,
            "revise_count": revise_count,
        }
        _write_verdict(workspace, slug, payload)

        # Read the root ADR to know how many domains we need verdicts on.
        root_adr_path = os.path.join(workspace, ROOT_ADR_PATH)
        if os.path.isfile(root_adr_path):
            with open(root_adr_path) as fh:
                domains = parse_domains(fh.read())
        else:
            domains = []
        expected_slugs = {d.get("slug") for d in domains if d.get("slug")}

        if effective == "approved":
            idx = next(
                (i + 1 for i, d in enumerate(domains) if d.get("slug") == slug),
                None,
            )
            if idx is not None:
                try:
                    promote_adr_to_docs(workspace, domain_adr_path(idx, slug))
                    promote_adr_to_docs(workspace, domain_grill_path(idx, slug))
                except Exception as exc:
                    log.warning(
                        "scaffold.domain_adr.promotion_failed",
                        task_id=task_id,
                        slug=slug,
                        error=str(exc),
                    )

        verdicts = _read_all_verdicts(workspace)
        any_revise = any(
            v.get("verdict") == "revise" for s_, v in verdicts.items() if s_ in expected_slugs
        )
        all_resolved = bool(expected_slugs) and all(
            s_ in verdicts and verdicts[s_].get("verdict") != "revise" for s_ in expected_slugs
        )

        if any_revise:
            await transition(
                s,
                task,
                TaskStatus.BUILDING_DOMAIN_ADRS,
                message=f"Domain {slug}: revise round {revise_count}",
            )
            await s.commit()
            log.info(
                "scaffold.domain_adr.revise",
                task_id=task_id,
                slug=slug,
                round=revise_count,
            )
            return TaskStatus.BUILDING_DOMAIN_ADRS

        if all_resolved:
            # ADR-019 T7 — insert AWAITING_REQUIRED_SECRETS gate between
            # Phase C (domain ADR approval) and Phase D (child dispatch).
            # The scaffold driver picks this up and runs check_secrets_gate.
            await transition(
                s,
                task,
                TaskStatus.AWAITING_REQUIRED_SECRETS,
                message=(
                    "All domain ADRs resolved — checking required secrets "
                    "before dispatching child trios"
                ),
            )
            await s.commit()
            log.info("scaffold.domain_adr.all_resolved", task_id=task_id)
            return TaskStatus.AWAITING_REQUIRED_SECRETS

        # Still waiting on at least one more domain verdict — no transition.
        await s.commit()
        log.info(
            "scaffold.domain_adr.partial",
            task_id=task_id,
            slug=slug,
            verdict=effective,
        )
        return task.status


async def request_po_verdicts(task: Task) -> list[str]:
    """Ask the PO standin to verdict every domain ADR (freeform mode).

    Reads the root ADR to discover the domain list, then for each
    domain reads ``.auto-agent/adrs/<idx>-<slug>.md`` and delegates to
    ``agent.po_agent.po_approve_domain_adr``. Returns the list of
    verdict-file absolute paths the PO wrote.

    Caller (parent driver) then invokes :func:`apply_verdict` once per
    slug with the payload it wrote. Caller is responsible for checking
    ``task.freeform_mode is True`` before invoking this.
    """

    from agent.po_agent import po_approve_domain_adr

    workspace = await prepare_scaffold_workspace(task)

    root_adr_abs = os.path.join(workspace, ROOT_ADR_PATH)
    if not os.path.isfile(root_adr_abs):
        log.warning(
            "scaffold.domain_adr.po_standin_root_adr_missing",
            task_id=task.id,
            path=root_adr_abs,
        )
        return []

    with open(root_adr_abs) as fh:
        root_adr_md = fh.read()

    domains = parse_domains(root_adr_md)
    if not domains:
        log.warning(
            "scaffold.domain_adr.po_standin_no_domains_parsed",
            task_id=task.id,
        )
        return []

    from agent.lifecycle.workspace_paths import domain_adr_path

    written: list[str] = []
    for idx, domain in enumerate(domains, start=1):
        slug = (domain.get("slug") or f"domain-{idx}").strip()
        adr_rel = domain_adr_path(idx, slug)
        adr_abs = os.path.join(workspace, adr_rel)
        adr_md = ""
        if os.path.isfile(adr_abs):
            try:
                with open(adr_abs) as fh:
                    adr_md = fh.read()
            except OSError:
                adr_md = ""

        await po_approve_domain_adr(task, adr_md, slug, workspace)

        verdict_abs = os.path.join(workspace, domain_adr_approval_path(slug))
        written.append(verdict_abs)
        log.info(
            "scaffold.domain_adr.po_standin_verdict_written",
            task_id=task.id,
            slug=slug,
            path=verdict_abs,
        )

    return written


__all__ = [
    "MAX_DOMAIN_REVISE_ROUNDS",
    "apply_verdict",
    "request_po_verdicts",
]
