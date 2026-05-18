"""Phase C — per-domain grill + architect, serial — ADR-018 §5.

For each domain parsed out of the root ADR's ``domains:`` YAML block we
run two agents serially:

1. **Domain grill** (``domain_grill.run``) — the always-grill principle
   from feedback-always-grill: a domain architect cannot produce its
   ADR without first interrogating the user about this specific
   domain's scope, constraints, and ambiguities. The grill agent writes
   ``.auto-agent/adrs/<idx>-<slug>.grill.md`` when complete, or pauses
   with a pending question (file under ``domain_grill_questions/``) and
   the parent driver parks at ``AWAITING_DOMAIN_GRILL``.

2. **Domain architect** — reads the grill summary + ``000-system.md`` +
   ``intent.md`` and writes ``.auto-agent/adrs/<idx>-<slug>.md`` via the
   ``submit-domain-adr`` skill, with a structural-validation retry.

The loop's "current domain index" is persisted on
``task.subtasks["scaffold"]["current_domain_idx"]`` so re-entry after a
grill pause knows which domain to resume on.

Return shape:
- ``{"status": "all_complete", "results": [...]}`` — every domain's
  grill + ADR are on disk. The driver advances to
  ``AWAITING_DOMAIN_ADR_APPROVAL``.
- ``{"status": "awaiting_grill", "domain_slug": "<slug>", "question": "..."}``
  — a grill agent paused on a question. The driver transitions to
  ``AWAITING_DOMAIN_GRILL`` and returns.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.scaffold import domain_grill
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.scaffold.prompts import DOMAIN_ARCHITECT_SYSTEM
from agent.lifecycle.scaffold.validators import (
    parse_domains,
    validate_domain_adr,
)
from agent.lifecycle.workspace_paths import (
    INTENT_PATH,
    ROOT_ADR_PATH,
    domain_adr_path,
    domain_grill_path,
)
from shared.database import async_session

if TYPE_CHECKING:
    from shared.models import Task

log = structlog.get_logger()


MAX_VALIDATION_RETRIES = 1

# ``Task.subtasks`` JSONB key for the per-domain progress index — must
# survive re-entry after a grill pause so we resume on the right domain.
_SCAFFOLD_KEY = "scaffold"
_CURRENT_DOMAIN_IDX_KEY = "current_domain_idx"


def _get_current_domain_idx(task: Task) -> int:
    """Return 0-based index of the next domain to process (defaults 0)."""

    bucket = (task.subtasks or {}) if isinstance(task.subtasks, dict) else {}
    scaffold = bucket.get(_SCAFFOLD_KEY) if isinstance(bucket, dict) else None
    if not isinstance(scaffold, dict):
        return 0
    raw = scaffold.get(_CURRENT_DOMAIN_IDX_KEY)
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


async def _persist_current_domain_idx(task_id: int, value: int) -> None:
    """Write ``task.subtasks['scaffold']['current_domain_idx'] = value``.

    Opens its own session + commits — the caller owns the surrounding
    re-entry control flow.
    """

    from shared.models import Task

    async with async_session() as s:
        live = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        bucket = live.subtasks if isinstance(live.subtasks, dict) else {}
        new_bucket = dict(bucket)
        scaffold = new_bucket.get(_SCAFFOLD_KEY) or {}
        if not isinstance(scaffold, dict):
            scaffold = {}
        new_scaffold = dict(scaffold)
        new_scaffold[_CURRENT_DOMAIN_IDX_KEY] = int(value)
        new_bucket[_SCAFFOLD_KEY] = new_scaffold
        live.subtasks = new_bucket
        await s.commit()


def _read_text(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


async def run(task: Task) -> dict[str, Any]:
    """Drive the grill + architect serial loop for every domain.

    Re-entrant: each invocation starts at the domain index persisted on
    ``task.subtasks['scaffold']['current_domain_idx']`` (0 on the first
    pass). On a grill pause we return early so the parent driver can
    park at ``AWAITING_DOMAIN_GRILL``; the next re-entry picks up at the
    same index (the grill summary file will be present so the grill
    short-circuits).
    """

    workspace = await prepare_scaffold_workspace(task)
    home_dir = await home_dir_for_task(task)

    root_adr_abs = os.path.join(workspace, ROOT_ADR_PATH)
    if not os.path.isfile(root_adr_abs):
        log.warning(
            "scaffold.domain_architect.root_adr_missing",
            task_id=task.id,
            path=root_adr_abs,
        )
        return {"status": "all_complete", "results": []}

    root_adr_md = _read_text(root_adr_abs)
    domains = parse_domains(root_adr_md)
    if not domains:
        log.warning(
            "scaffold.domain_architect.no_domains_parsed",
            task_id=task.id,
        )
        return {"status": "all_complete", "results": []}

    intent_text = _read_text(os.path.join(workspace, INTENT_PATH))

    start_idx = _get_current_domain_idx(task)
    if start_idx < 0:
        start_idx = 0

    results: list[dict] = []

    for loop_idx, domain in enumerate(domains):
        if loop_idx < start_idx:
            # Already processed in a previous re-entry — collect its result
            # from disk so the final return is complete.
            idx = loop_idx + 1
            slug = domain.get("slug") or f"domain-{idx}"
            entry = dict(domain)
            entry["index"] = idx
            entry["adr_path"] = domain_adr_path(idx, slug)
            entry["validation"] = "ok"
            results.append(entry)
            continue

        idx = loop_idx + 1
        slug = domain.get("slug") or f"domain-{idx}"
        name = domain.get("name") or slug

        # ------------------------------------------------------------------
        # Step 1: domain-grill round for this domain.
        # ------------------------------------------------------------------

        grill_outcome = await domain_grill.run(
            task,
            {
                "name": name,
                "slug": slug,
                "scope_summary": domain.get("scope_summary") or "",
                "index": idx,
            },
        )

        if grill_outcome.get("status") == "awaiting_user":
            # Persist progress so re-entry resumes here, then bubble up.
            await _persist_current_domain_idx(task.id, loop_idx)
            log.info(
                "scaffold.domain_architect.grill_paused",
                task_id=task.id,
                slug=slug,
                index=idx,
            )
            return {
                "status": "awaiting_grill",
                "domain_slug": slug,
                "question": grill_outcome.get("question", ""),
            }

        # On "degraded" or "summary_written" we proceed to the architect.
        # A degraded grill is logged but not fatal — the architect's
        # prompt will fall back to scope_summary alone.

        # ------------------------------------------------------------------
        # Step 2: domain architect writes the ADR.
        # ------------------------------------------------------------------

        target_rel = domain_adr_path(idx, slug)
        target_abs = os.path.join(workspace, target_rel)
        os.makedirs(os.path.dirname(target_abs), exist_ok=True)

        grill_summary_rel = domain_grill_path(idx, slug)
        grill_summary_abs = os.path.join(workspace, grill_summary_rel)
        grill_summary_text = _read_text(grill_summary_abs)

        system_prompt = DOMAIN_ARCHITECT_SYSTEM.format(
            domain_name=name,
            domain_slug=slug,
            index=idx,
        )

        prompt = (
            f"You are the domain architect for **{name}** "
            f"(slug `{slug}`, index {idx}).\n\n"
            "The root ADR placed this domain in the system as:\n\n"
            "----- BEGIN ROOT ADR -----\n"
            f"{root_adr_md}\n"
            "----- END ROOT ADR -----\n\n"
            "----- BEGIN INTENT -----\n"
            f"{intent_text or '(intent.md missing)'}\n"
            "----- END INTENT -----\n\n"
            "----- BEGIN DOMAIN GRILL SUMMARY (authoritative for this domain) -----\n"
            f"{grill_summary_text or '(grill summary missing — fall back to root scope_summary)'}\n"
            "----- END DOMAIN GRILL SUMMARY -----\n\n"
            f"Your domain's scope summary from the root ADR:\n"
            f"> {domain.get('scope_summary') or ''}\n\n"
            "Treat the grill summary as the user's voice. Where it conflicts "
            "with your instinct, follow the grill summary. Write the domain "
            f"ADR via the `submit-domain-adr` skill. Target path: `{target_rel}`."
        )

        agent = create_agent(
            workspace=workspace,
            task_id=task.id,
            task_description=task.description or "",
            repo_name=task.repo.name if task.repo else None,
            home_dir=home_dir,
            org_id=task.organization_id,
            max_turns=40,
        )

        await agent.run(prompt, system=system_prompt)

        validation_label: str | list[str] = "ok"
        for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
            adr_md = _read_text(target_abs)
            result = validate_domain_adr(adr_md)
            if result.ok:
                break
            if attempt > MAX_VALIDATION_RETRIES:
                validation_label = list(result.errors)
                log.warning(
                    "scaffold.domain_architect.validation_failed",
                    task_id=task.id,
                    slug=slug,
                    errors=result.errors,
                )
                break
            retry_prompt = (
                "Your domain ADR failed structural validation. Fix "
                "every error below and re-submit via `submit-domain-adr` "
                "(overwriting the same file). Do not output the ADR in "
                "chat — just call the skill and stop.\n\n"
                "Errors:\n" + "\n".join(f"- {e}" for e in result.errors)
            )
            await agent.run(retry_prompt, system=system_prompt, resume=True)

        entry = dict(domain)
        entry["index"] = idx
        entry["adr_path"] = target_rel
        entry["validation"] = validation_label
        results.append(entry)

        log.info(
            "scaffold.domain_architect.completed_one",
            task_id=task.id,
            slug=slug,
            index=idx,
            path=target_rel,
        )

        # Bump progress so a re-entry mid-loop (rare, but defensive) doesn't
        # re-process this domain.
        await _persist_current_domain_idx(task.id, loop_idx + 1)

    return {"status": "all_complete", "results": results}


__all__ = ["run"]
