"""Phase C-grill — per-domain grill round — ADR-018 §5 (Stage 8).


Each domain ADR gets its own grill round BEFORE the architect writes the
ADR. The grill agent reads the root ADR + intent + the domain entry,
then either:

- asks the user a clarifying question via the `submit-domain-grill-question`
  skill (the file lands at ``.auto-agent/domain_grill_questions/<slug>.json``,
  the parent driver transitions to ``AWAITING_DOMAIN_GRILL`` and returns;
  the router endpoint re-invokes the driver on user answer), or

- writes the grill summary via the `submit-domain-grill-summary` skill
  (markdown at ``.auto-agent/adrs/<idx>-<slug>.grill.md``).

When ``task.freeform_mode is True``, ``agent.po_agent.po_answer_domain_grill``
answers any pending question instead of escaping to the user — same
pattern as :mod:`agent.lifecycle.scaffold.intent_grill`.

The return value tells the caller which way the grill went:

- ``{"status": "summary_written", "summary_path": "<rel>"}`` — the agent
  produced a grill summary, the caller can proceed to the architect.
- ``{"status": "awaiting_user", "domain_slug": "<slug>", "question": "..."}``
  — the agent asked a question and stopped; caller must park the parent
  in ``AWAITING_DOMAIN_GRILL`` and return.
- ``{"status": "degraded"}`` — agent terminated without writing either
  file; caller logs a warning and may decide to advance anyway (the
  validator on the architect side will fail-fast if the input is
  unusable).
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import TYPE_CHECKING, Any

import structlog

from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.scaffold.prompts import DOMAIN_GRILL_SYSTEM
from agent.lifecycle.workspace_paths import (
    INTENT_PATH,
    ROOT_ADR_PATH,
    domain_grill_answer_path,
    domain_grill_path,
    domain_grill_question_path,
)

if TYPE_CHECKING:
    from shared.models import Task

log = structlog.get_logger()


async def request_po_domain_answer(
    task: Task,
    question: str,
    domain_slug: str,
) -> str:
    """Ask the PO standin to answer a pending domain-grill question.

    Thin wrapper around ``agent.po_agent.po_answer_domain_grill`` so the
    scaffold modules don't have to know about ``po_agent`` directly.
    Returns the absolute path the PO wrote to.

    Caller is responsible for checking ``task.freeform_mode`` before
    invoking this — in human-in-loop mode the user answers via the UI.
    """

    from agent.po_agent import po_answer_domain_grill

    workspace = await prepare_scaffold_workspace(task)
    await po_answer_domain_grill(task, question, domain_slug, workspace)
    return os.path.join(workspace, domain_grill_answer_path(domain_slug))


def _read_pending_question(workspace: str, slug: str) -> str | None:
    """Return the question string if a pending question file exists.

    The question file is written by the agent's
    ``submit-domain-grill-question`` skill. We treat a question file as
    "pending" only when no matching answer file exists yet — once the
    user (or PO) answers, the orchestrator writes the answer file and
    the driver re-enters; the agent's next turn consumes both.
    """

    q_abs = os.path.join(workspace, domain_grill_question_path(slug))
    if not os.path.isfile(q_abs):
        return None
    a_abs = os.path.join(workspace, domain_grill_answer_path(slug))
    if os.path.isfile(a_abs):
        # An answer is already on disk — not pending. The next agent
        # turn will pair them up.
        return None
    try:
        with open(q_abs) as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    question = payload.get("question")
    return str(question) if question else None


def _clear_question_and_answer(workspace: str, slug: str) -> None:
    """Remove any prior question/answer pair so a new grill turn starts clean.

    Called before re-entering the agent so a stale "pending question"
    file from an earlier turn isn't mistaken for the current one.
    """

    for rel in (domain_grill_question_path(slug), domain_grill_answer_path(slug)):
        abs_path = os.path.join(workspace, rel)
        if os.path.isfile(abs_path):
            with contextlib.suppress(OSError):
                os.remove(abs_path)


async def run(task: Task, domain: dict[str, Any]) -> dict[str, Any]:
    """Run the grill agent for one domain.

    ``domain`` is one entry from the parsed root-ADR domains list with
    at least ``slug``, ``name``, ``scope_summary``, and ``index`` keys.

    Returns one of:
      - ``{"status": "summary_written", "summary_path": "<rel>"}``
      - ``{"status": "awaiting_user", "domain_slug": "<slug>", "question": "..."}``
      - ``{"status": "degraded", "domain_slug": "<slug>"}``
    """

    slug = domain.get("slug") or "domain"
    name = domain.get("name") or slug
    index = int(domain.get("index") or 1)
    scope_summary = domain.get("scope_summary") or ""

    workspace = await prepare_scaffold_workspace(task)
    home_dir = await home_dir_for_task(task)

    summary_rel = domain_grill_path(index, slug)
    summary_abs = os.path.join(workspace, summary_rel)
    os.makedirs(os.path.dirname(summary_abs), exist_ok=True)

    # If the summary already exists (e.g. previous turn finished and the
    # driver is re-entered to advance) we're done — short-circuit so we
    # don't re-burn LLM tokens.
    if os.path.isfile(summary_abs):
        log.info(
            "scaffold.domain_grill.summary_already_present",
            task_id=task.id,
            slug=slug,
            path=summary_rel,
        )
        return {"status": "summary_written", "summary_path": summary_rel}

    intent_present = os.path.isfile(os.path.join(workspace, INTENT_PATH))
    root_adr_present = os.path.isfile(os.path.join(workspace, ROOT_ADR_PATH))

    # If there's a fresh answer file from a previous pause, surface it to
    # the agent's prompt; otherwise the agent runs as a first-pass grill.
    answer_text: str | None = None
    answer_abs = os.path.join(workspace, domain_grill_answer_path(slug))
    if os.path.isfile(answer_abs):
        try:
            with open(answer_abs) as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                answer_text = str(payload.get("answer") or "")
        except (OSError, json.JSONDecodeError):
            answer_text = None

    # Wipe the previous pending question (if any) before running so a
    # stale file from a prior turn doesn't masquerade as the new one.
    _clear_question_and_answer(workspace, slug)

    system_prompt = DOMAIN_GRILL_SYSTEM.format(
        domain_name=name,
        domain_slug=slug,
        index=index,
    )

    freeform = bool(getattr(task, "freeform_mode", False))
    standin_hint = (
        (
            "\n\nFREEFORM MODE: there is no human in the loop. When you would "
            "normally pause to ask the user a question, instead infer the most "
            "reasonable answer from the intent.md + root ADR + scope summary "
            "below and proceed. Aim to write the grill summary in one pass."
        )
        if freeform
        else ""
    )

    prior_answer_block = (
        (
            "\n\n----- BEGIN PRIOR USER ANSWER (to your last question) -----\n"
            f"{answer_text}\n"
            "----- END PRIOR USER ANSWER -----\n"
            "If this answer resolves all open questions, write the grill summary "
            "via `submit-domain-grill-summary`. Otherwise, ask a follow-up via "
            "`submit-domain-grill-question`."
        )
        if answer_text
        else ""
    )

    intent_line = (
        f"Read `{INTENT_PATH}` — it states what the user wants for the overall product."
        if intent_present else f"`{INTENT_PATH}` is missing."
    )
    root_adr_line = (
        f"Read `{ROOT_ADR_PATH}` — it places this domain in the wider system."
        if root_adr_present else f"`{ROOT_ADR_PATH}` is missing."
    )

    prompt = (
        f"You are running the per-domain grill for **{name}** "
        f"(slug `{slug}`, index {index}).\n\n"
        f"Your domain's scope summary from the root ADR:\n> {scope_summary}\n\n"
        f"{intent_line}\n"
        f"{root_adr_line}\n"
        + prior_answer_block
        + standin_hint
        + "\n\nGrill the user (or freeform standin) until you can write a "
        f"complete grill summary at `{summary_rel}`. When you need an answer "
        "from the user, call `submit-domain-grill-question` and stop. When "
        "you're done, call `submit-domain-grill-summary` and stop."
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

    # Outcome detection: summary first, then pending question, then degraded.
    if os.path.isfile(summary_abs):
        log.info(
            "scaffold.domain_grill.summary_written",
            task_id=task.id,
            slug=slug,
            path=summary_rel,
        )
        return {"status": "summary_written", "summary_path": summary_rel}

    pending = _read_pending_question(workspace, slug)
    if pending is not None:
        log.info(
            "scaffold.domain_grill.awaiting_user",
            task_id=task.id,
            slug=slug,
            preview=pending[:160],
        )
        return {
            "status": "awaiting_user",
            "domain_slug": slug,
            "question": pending,
        }

    log.warning(
        "scaffold.domain_grill.degraded_no_output",
        task_id=task.id,
        slug=slug,
    )
    return {"status": "degraded", "domain_slug": slug}


__all__ = [
    "request_po_domain_answer",
    "run",
]
