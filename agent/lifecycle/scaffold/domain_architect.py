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
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.scaffold import domain_grill
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.scaffold.prompts import domain_architect_system
from agent.lifecycle.scaffold.required_secrets import (
    parse_manifest_file,
    read_all_manifests,
    reconcile,
)
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
from shared import repo_secrets
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


def _workspace_as_path(workspace: str) -> Path:
    """Convert the workspace string to a ``pathlib.Path``."""
    return Path(workspace)


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
        grill_summary_present = os.path.isfile(grill_summary_abs)

        # -- Collect repo-secret state so the prompt can show what's already set.
        workspace_path = _workspace_as_path(workspace)
        currently_set: list[str] = []
        already_declared: list[tuple[str, str]] = []
        if task.repo_id and task.organization_id:
            try:
                key_rows = await repo_secrets.list_keys(
                    task.repo_id,
                    organization_id=task.organization_id,
                )
                currently_set = [row["key"] for row in key_rows if row.get("set")]
            except Exception:
                # Non-fatal: prompt degrades gracefully to "(none set)".
                pass
            # Read sibling manifests already on disk (previously written domains).
            sibling_manifests = read_all_manifests(workspace_path)
            for manifest in sibling_manifests:
                if manifest.domain != slug:
                    for entry_item in manifest.secrets:
                        already_declared.append((entry_item.key, manifest.domain))

        system_prompt = domain_architect_system(
            domain_name=name,
            domain_slug=slug,
            index=idx,
            currently_set=currently_set,
            already_declared=already_declared,
        )

        grill_hint = (
            f"Read `{grill_summary_rel}` — it is authoritative for this domain "
            "and is the user's voice. Where it conflicts with your instinct, "
            "follow the grill summary."
            if grill_summary_present
            else f"`{grill_summary_rel}` is missing — fall back to the scope summary below."
        )

        prompt = (
            f"You are the domain architect for **{name}** "
            f"(slug `{slug}`, index {idx}).\n\n"
            f"Read `{ROOT_ADR_PATH}` for the system decomposition and "
            f"`{INTENT_PATH}` for the original user intent.\n\n"
            f"{grill_hint}\n\n"
            f"Your domain's scope summary from the root ADR:\n"
            f"> {domain.get('scope_summary') or ''}\n\n"
            "Write the domain ADR via the `submit-domain-adr` skill. "
            f"Target path: `{target_rel}`."
        )

        # Allocate a per-domain session_id so validation retries below
        # (and the secrets-manifest retry) actually --resume the prior CLI
        # session. See plans/2026-05-26-scaffold-token-savings.md Phase 2.
        session_id = f"scaffold-{task.id}-domain-architect-{slug}"
        agent = create_agent(
            workspace=workspace,
            session_id=session_id,
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

        # -- Post-run: validate the required-secrets manifest if the architect
        #    wrote one, then ALWAYS run reconcile so DB rows stay in sync.
        req_secrets_rel = f".auto-agent/required_secrets/{slug}.json"
        req_secrets_abs = os.path.join(workspace, req_secrets_rel)
        if os.path.isfile(req_secrets_abs):
            manifest_path = Path(req_secrets_abs)
            for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
                try:
                    parse_manifest_file(manifest_path)
                    break
                except (ValueError, Exception) as exc:
                    if attempt > MAX_VALIDATION_RETRIES:
                        log.warning(
                            "scaffold.domain_architect.secrets_manifest_invalid",
                            task_id=task.id,
                            slug=slug,
                            error=str(exc),
                        )
                        raise
                    retry_secrets_prompt = (
                        "Your required-secrets manifest failed validation. Fix "
                        "the errors below and re-submit via `submit-required-secrets` "
                        "(overwriting the same file). Do not output the manifest in "
                        "chat — just call the skill and stop.\n\n"
                        f"Error: {exc}"
                    )
                    await agent.run(retry_secrets_prompt, system=system_prompt, resume=True)

        # Reconcile runs unconditionally — even when no manifest was written this
        # round.  This ensures that keys dropped by a revise pass get demoted
        # rather than lingering as stale architect_required rows.
        if task.repo_id and task.organization_id:
            try:
                report = await reconcile(
                    workspace_path,
                    repo_id=task.repo_id,
                    organization_id=task.organization_id,
                )
                log.info(
                    "scaffold.domain_architect.reconcile_complete",
                    task_id=task.id,
                    slug=slug,
                    promoted=report.promoted,
                    demoted=report.demoted,
                    created=report.created,
                    unchanged=report.unchanged,
                )
            except Exception as exc:
                log.warning(
                    "scaffold.domain_architect.reconcile_failed",
                    task_id=task.id,
                    slug=slug,
                    error=str(exc),
                )

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
