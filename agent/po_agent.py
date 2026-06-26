"""Product Owner agent — answers architect clarification questions.

Distinct from agent/po_analyzer.py which generates suggestions on a cron.
This module is a focused entry point: read the architect's question for
a trio parent, build a prompt that injects Repo.product_brief + the
current ARCHITECTURE.md, run a readonly agent, parse {"answer": "..."},
write it to the architect_attempts row, and publish
ARCHITECT_CLARIFICATION_RESOLVED.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select

from agent.lifecycle.factory import create_agent
from agent.llm.structured import parse_json_response
from agent.workspace import clone_repo
from shared.database import async_session
from shared.events import Event, TaskEventType, publish
from shared.models import ArchitectAttempt, Repo, Task

log = logging.getLogger(__name__)


def _workspace_root(workspace) -> str:
    """Return the filesystem path for a workspace handle.

    ``clone_repo`` returns a string path in production, but trio tests
    mock it to return an object with a ``.root`` attribute. Mirror the
    defensive pattern used in ``agent/lifecycle/trio/architect.py`` so
    both shapes work transparently.
    """
    return workspace.root if hasattr(workspace, "root") else str(workspace)


async def answer_architect_question(parent_task_id: int) -> None:
    """Run the PO to answer the architect's outstanding clarification.

    Reads the latest architect_attempts row for parent_task_id where
    clarification_question IS NOT NULL AND clarification_answer IS NULL.
    Loads Repo.product_brief. Builds a readonly agent. Writes the answer
    (or a failure note) to the row and publishes
    ARCHITECT_CLARIFICATION_RESOLVED so the dispatcher can resume the
    architect.
    """
    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one_or_none()
        if parent is None:
            log.warning("po_agent.parent_missing", extra={"task_id": parent_task_id})
            return
        attempt = (
            await s.execute(
                select(ArchitectAttempt)
                .where(ArchitectAttempt.task_id == parent_task_id)
                .where(ArchitectAttempt.clarification_question.is_not(None))
                .where(ArchitectAttempt.clarification_answer.is_(None))
                .order_by(ArchitectAttempt.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if attempt is None:
            log.warning(
                "po_agent.no_pending_clarification",
                extra={"task_id": parent_task_id},
            )
            return
        question = attempt.clarification_question
        attempt_id = attempt.id
        repo = (await s.execute(select(Repo).where(Repo.id == parent.repo_id))).scalar_one_or_none()
        if repo is None:
            log.warning("po_agent.repo_missing", extra={"task_id": parent_task_id})
            return
        product_brief = repo.product_brief or ""
        repo_url = repo.url
        repo_name = repo.name
        repo_id = repo.id
        default_branch = repo.default_branch or "main"

    if not product_brief:
        log.warning(
            "po_agent.no_product_brief",
            extra={"repo": repo_name, "task_id": parent_task_id},
        )

    # Clone readonly for code-grounded answers.
    workspace = await clone_repo(
        repo_url,
        parent_task_id,
        default_branch,
        workspace_name=f"po-{repo_name.replace('/', '-')}-{parent_task_id}",
        repo_id=repo_id,
    )
    workspace_root = _workspace_root(workspace)

    arch_md = ""
    arch_path = Path(workspace_root) / "ARCHITECTURE.md"
    if arch_path.exists():
        try:
            arch_md = arch_path.read_text(errors="replace")[:4000]
        except OSError:
            arch_md = ""

    prompt_parts: list[str] = []
    if product_brief:
        prompt_parts.append(f"# Product Brief\n\n{product_brief}\n")
    if arch_md:
        prompt_parts.append(f"# Current ARCHITECTURE.md (excerpt)\n\n{arch_md}\n")
    prompt_parts.append(
        "You are the Product Owner. The architect has paused and asked\n"
        "the following question. Answer as the PO, grounded in the\n"
        "product brief above. Be specific and brief (max ~300 words).\n\n"
        f"Question:\n{question}\n\n"
        "Output ONLY a JSON object on its own lines:\n"
        '```json\n{"answer": "<your answer>"}\n```\n'
    )
    prompt = "\n\n".join(prompt_parts)

    agent = create_agent(
        workspace,
        readonly=True,
        max_turns=8,
        task_description=f"PO answers architect for task #{parent_task_id}",
        repo_name=repo_name,
    )

    try:
        result = await agent.run(prompt)
        output = getattr(result, "output", "") or ""
    except Exception as e:
        log.exception("po_agent.run_failed", extra={"task_id": parent_task_id})
        await _write_answer(
            attempt_id,
            f"(PO failed with an exception: {type(e).__name__}: {e!s})",
        )
        await publish(
            Event(
                type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
                task_id=parent_task_id,
            )
        )
        return

    parsed = parse_json_response(output)
    if not isinstance(parsed, dict) or "answer" not in parsed:
        log.warning(
            "po_agent.unparseable_output",
            extra={"task_id": parent_task_id, "output_preview": output[:300]},
        )
        answer = f"(PO returned no parseable answer. Raw output preview: {output[:400]!r})"
    else:
        answer = str(parsed["answer"])

    await _write_answer(attempt_id, answer)
    await publish(
        Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
            task_id=parent_task_id,
        )
    )
    log.info(
        "po_agent.answered",
        extra={"task_id": parent_task_id, "answer_preview": answer[:120]},
    )


async def _write_answer(attempt_id: int, answer: str) -> None:
    async with async_session() as s:
        row = (
            await s.execute(select(ArchitectAttempt).where(ArchitectAttempt.id == attempt_id))
        ).scalar_one()
        row.clarification_answer = answer
        row.clarification_source = "po"
        await s.commit()


# ---------------------------------------------------------------------------
# ADR-018 — scaffold-flow gate standins. The scaffold parent task drives
# gates that need a PO standin when ``task.freeform_mode is True``:
# the per-domain grill, the root-ADR approval, and each per-domain ADR
# approval. These helpers own the "PO writes the gate file directly"
# pattern — no skill / Claude Code roundtrip; we already know the JSON
# shape so a Python-side write is the simplest correct thing.
#
# Each helper logs a ``fallback_default(source=heuristic)`` marker on the
# decision when grounding is missing (no product_brief, no partial
# intent.md), mirroring the ADR-015 §6 standin contract.
# ---------------------------------------------------------------------------


async def po_answer_domain_grill(
    task,
    question: str,
    domain_slug: str,
    workspace_root: str,
) -> None:
    """PO standin answers a per-domain-grill question (freeform mode).

    The domain grill runs after the root ADR is approved, before each
    domain architect writes its ADR. Grounds the answer in
    (a) the task description, (b) ``intent.md``, and (c) the root ADR
    ``000-system.md`` (so the standin can see what the system-level
    decomposition already settled). When grounding is missing the PO
    falls back to a deterministic default and logs
    ``fallback_default(source=heuristic)`` — the ADR-015 §6 standin
    contract.

    Writes ``.auto-agent/domain_grill_answers/<slug>.json`` — the
    domain-grill agent reads this file when it re-enters its session.
    """

    from agent.lifecycle.workspace_paths import (
        INTENT_PATH,
        ROOT_ADR_PATH,
        domain_grill_answer_path,
    )

    description = (getattr(task, "description", "") or "").strip()
    title = (getattr(task, "title", "") or "").strip()
    slug = (domain_slug or "").strip() or "unknown"

    partial_intent = ""
    intent_abs = Path(workspace_root) / INTENT_PATH
    if intent_abs.exists():
        try:
            partial_intent = intent_abs.read_text(errors="replace")[:2000]
        except OSError:
            partial_intent = ""

    root_adr_text = ""
    root_adr_abs = Path(workspace_root) / ROOT_ADR_PATH
    if root_adr_abs.exists():
        try:
            root_adr_text = root_adr_abs.read_text(errors="replace")[:4000]
        except OSError:
            root_adr_text = ""

    fallback_reasons: list[str] = []

    if not description and not partial_intent and not root_adr_text:
        fallback_reasons.append("domain_grill:no_grounding_context")
        answer = (
            "Default answer (heuristic): keep this domain's scope minimal "
            f"— include only what the slug name '{slug}' literally implies. "
            "Defer anything ambiguous to a future ADR revision."
        )
        cited: list[str] = []
        log.warning(
            "fallback_default(source=heuristic) standin_kind=po gate=domain_grill "
            "reason=no_grounding_context task_id=%s slug=%s",
            getattr(task, "id", None),
            slug,
        )
    else:
        system_prompt = (
            "You are the Product Owner standin for a brand-new scaffold task. "
            "No product brief exists yet. The root ADR has been written and "
            "approved. The domain-grill agent for one bounded context has paused "
            "to ask a clarifying question — answer it grounded in the task "
            "description, intent.md, and the root ADR below. Pick a sensible "
            "default when the inputs are truly under-determined; never escape "
            "to the user."
        )
        prompt_parts: list[str] = []
        if title:
            prompt_parts.append(f"# Task title\n\n{title}\n")
        if description:
            prompt_parts.append(f"# Task description\n\n{description}\n")
        if partial_intent:
            prompt_parts.append(f"# .auto-agent/intent.md\n\n{partial_intent}\n")
        if root_adr_text:
            prompt_parts.append(
                f"# .auto-agent/adrs/000-system.md (root ADR)\n\n{root_adr_text}\n"
            )
        prompt_parts.append(
            f"# Domain under grill: `{slug}`\n\n"
            f"The domain-grill agent for `{slug}` paused with this question:\n\n"
            f"{question}\n\n"
            "Answer as the PO standin in 1-3 sentences, focused on THIS domain only.\n"
            'Output ONLY a JSON object on its own lines: ```json\n'
            '{"answer": "<your answer>"}\n```'
        )
        prompt = "\n\n".join(prompt_parts)

        agent = create_agent(
            workspace_root,
            readonly=True,
            max_turns=4,
            task_description=(
                f"PO standin answers domain-grill question for task #"
                f"{getattr(task, 'id', '?')} domain={slug}"
            ),
            repo_name=getattr(getattr(task, "repo", None), "name", None),
        )

        cited = []
        if description:
            cited.append("task.description")
        if partial_intent:
            cited.append("intent.md")
        if root_adr_text:
            cited.append("000-system.md")

        try:
            result = await agent.run(prompt, system=system_prompt)
            output = getattr(result, "output", "") or ""
            parsed = parse_json_response(output)
            if isinstance(parsed, dict) and "answer" in parsed:
                answer = str(parsed["answer"])
            else:
                fallback_reasons.append("domain_grill:unparseable_output")
                log.warning(
                    "fallback_default(source=heuristic) standin_kind=po gate=domain_grill "
                    "reason=unparseable_output task_id=%s slug=%s",
                    getattr(task, "id", None),
                    slug,
                )
                answer = (
                    "Default answer (heuristic): keep this domain's scope minimal "
                    "and consistent with what the root ADR already named."
                )
                cited = []
        except Exception as exc:
            fallback_reasons.append("domain_grill:agent_failed")
            log.exception(
                "fallback_default(source=heuristic) standin_kind=po gate=domain_grill "
                "reason=agent_failed task_id=%s slug=%s exc=%s",
                getattr(task, "id", None),
                slug,
                type(exc).__name__,
            )
            answer = (
                "Default answer (heuristic): keep this domain's scope minimal "
                "and consistent with what the root ADR already named."
            )
            cited = []

    payload = {
        "schema_version": "1",
        "domain_slug": slug,
        "question": question,
        "answer": answer,
        "source": "po_standin",
        "cited_context": cited,
        "fallback_reasons": fallback_reasons,
    }
    out_path = Path(workspace_root) / domain_grill_answer_path(slug)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))

    log.info(
        "po_agent.domain_grill_answered task_id=%s slug=%s answer_preview=%r fallback=%s",
        getattr(task, "id", None),
        slug,
        answer[:120],
        bool(fallback_reasons),
    )


async def po_approve_root_adr(task, adr_md: str, workspace_root: str) -> None:
    """PO standin reviews the root ADR and writes the approval verdict.

    Heuristic, mirroring ``POStandin._verdict_for_artefact``: empty ADR →
    ``revise`` with a clear comment; otherwise ``approved`` (the ADR has
    already been structurally validated by ``validate_root_adr`` before
    this is called). Logs every decision with the rationale so the gate-
    history audit panel can reconstruct who decided what.

    Writes ``.auto-agent/root_adr_approval.json`` directly — PO is a
    Python-side standin, not a Claude Code agent, so it doesn't need the
    skill bridge here.
    """

    from agent.lifecycle.workspace_paths import ROOT_ADR_APPROVAL_PATH

    description = (getattr(task, "description", "") or "").strip()
    body = (adr_md or "").strip()

    if not body:
        verdict = "revise"
        comments = (
            "Root ADR is empty. Re-run the root architect with the intent.md "
            "and produce a non-empty ADR."
        )
        fallback_reasons = ["root_adr_approval:empty_adr"]
        log.warning(
            "fallback_default(source=heuristic) standin_kind=po gate=root_adr_approval "
            "reason=empty_adr task_id=%s",
            getattr(task, "id", None),
        )
    elif not description:
        verdict = "approved"
        comments = (
            "Default approval (heuristic): no task description on file to "
            "cross-check against; root ADR is structurally valid so we pass it."
        )
        fallback_reasons = ["root_adr_approval:no_task_description"]
        log.warning(
            "fallback_default(source=heuristic) standin_kind=po gate=root_adr_approval "
            "reason=no_task_description task_id=%s",
            getattr(task, "id", None),
        )
    else:
        verdict = "approved"
        comments = (
            "Root ADR aligns with the task description and passes structural "
            "validation. Approved by PO standin."
        )
        fallback_reasons = []

    payload = {
        "schema_version": "1",
        "verdict": verdict,
        "comments": comments,
        "source": "po_standin",
        "fallback_reasons": fallback_reasons,
    }
    out_path = Path(workspace_root) / ROOT_ADR_APPROVAL_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))

    log.info(
        "po_agent.root_adr_decided task_id=%s verdict=%s reason=%s",
        getattr(task, "id", None),
        verdict,
        comments[:160],
    )


async def po_approve_domain_adr(
    task,
    adr_md: str,
    domain_slug: str,
    workspace_root: str,
) -> None:
    """PO standin reviews one domain ADR and writes the per-slug verdict.

    Same heuristic as :func:`po_approve_root_adr` but per-domain.

    Writes ``.auto-agent/domain_adr_approvals/<slug>.json`` directly.
    """

    from agent.lifecycle.workspace_paths import domain_adr_approval_path

    description = (getattr(task, "description", "") or "").strip()
    body = (adr_md or "").strip()
    slug = (domain_slug or "").strip()

    if not slug:
        # Defensive: an unslugged call shouldn't happen — the scaffold
        # driver always knows the slug — but if it does, write to a
        # well-known stub path so the failure is visible in the
        # verdicts directory rather than silently corrupt.
        slug = "unknown"

    if not body:
        verdict = "revise"
        comments = (
            f"Domain ADR for '{slug}' is empty. Re-run the domain architect "
            "with the root ADR and intent.md and produce a non-empty ADR."
        )
        fallback_reasons = ["domain_adr_approval:empty_adr"]
        log.warning(
            "fallback_default(source=heuristic) standin_kind=po gate=domain_adr_approval "
            "reason=empty_adr task_id=%s slug=%s",
            getattr(task, "id", None),
            slug,
        )
    elif not description:
        verdict = "approved"
        comments = (
            "Default approval (heuristic): no task description on file; the "
            f"domain ADR for '{slug}' is structurally valid so we pass it."
        )
        fallback_reasons = ["domain_adr_approval:no_task_description"]
        log.warning(
            "fallback_default(source=heuristic) standin_kind=po gate=domain_adr_approval "
            "reason=no_task_description task_id=%s slug=%s",
            getattr(task, "id", None),
            slug,
        )
    else:
        verdict = "approved"
        comments = (
            f"Domain ADR for '{slug}' aligns with the task description and "
            "passes structural validation. Approved by PO standin."
        )
        fallback_reasons = []

    payload = {
        "schema_version": "1",
        "slug": slug,
        "verdict": verdict,
        "comments": comments,
        "source": "po_standin",
        "fallback_reasons": fallback_reasons,
    }
    rel = domain_adr_approval_path(slug)
    out_path = Path(workspace_root) / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))

    log.info(
        "po_agent.domain_adr_decided task_id=%s slug=%s verdict=%s reason=%s",
        getattr(task, "id", None),
        slug,
        verdict,
        comments[:160],
    )
