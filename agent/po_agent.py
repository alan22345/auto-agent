"""Product Owner agent — answers architect clarification questions.

Distinct from agent/po_analyzer.py which generates suggestions on a cron.
This module is a focused entry point: read the architect's question for
a trio parent, build a prompt that injects Repo.product_brief + the
current ARCHITECTURE.md, run a readonly agent, parse {"answer": "..."},
write it to the architect_attempts row, and publish
ARCHITECT_CLARIFICATION_RESOLVED.
"""

from __future__ import annotations

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
# Phase 10 freeform-mode gate entry points — ADR-015 §6.
#
# These thin wrappers delegate to ``agent.lifecycle.standin.POStandin`` so
# every "PO acts as standin at gate X" call site has one shape. The
# ``POStandin`` class owns the gate-file write + decision-event logging
# invariants; the wrappers exist so callers don't have to know about the
# class.
#
# The orchestrator's preferred entry point is
# ``agent.lifecycle.standin.run_freeform_gate`` (which routes by mode +
# origin); these per-function helpers are for code paths that already
# know they want a PO decision specifically.
# ---------------------------------------------------------------------------


async def po_answer_grill(task, question: str, workspace_root: str) -> None:
    """PO standin answers a grill question (freeform mode).

    Reads ``Repo.product_brief`` + optional ``ARCHITECTURE.md`` excerpt
    as grounding. Writes ``.auto-agent/grill_answer.json`` and publishes
    a ``standin.decision`` event. Never escapes to the user — when
    context is missing, falls back to a heuristic default and logs the
    ``fallback_default(source=heuristic)`` marker.
    """

    from agent.lifecycle.standin import POStandin

    repo = await _load_repo(task.repo_id)
    standin = POStandin(task=task, repo=repo)
    await standin.answer_grill(question, {"workspace_root": workspace_root})


async def po_approve_plan(task, plan_md: str, workspace_root: str) -> None:
    """PO standin approves/rejects a complex-flow plan.

    Writes ``.auto-agent/plan_approval.json``. Never escapes.
    """

    from agent.lifecycle.standin import POStandin

    repo = await _load_repo(task.repo_id)
    standin = POStandin(task=task, repo=repo)
    await standin.approve_plan(plan_md, {"workspace_root": workspace_root})


async def po_approve_design(task, design_md: str, workspace_root: str) -> None:
    """PO standin approves/rejects a complex_large design doc.

    Same shape as :func:`po_approve_plan`; design approval reuses
    ``plan_approval.json`` per ADR-015 §2.
    """

    from agent.lifecycle.standin import POStandin

    repo = await _load_repo(task.repo_id)
    standin = POStandin(task=task, repo=repo)
    await standin.approve_design(design_md, {"workspace_root": workspace_root})


async def po_review_pr(task, pr_diff: str, pr_metadata: dict, workspace_root: str) -> None:
    """PO standin reviews a PR and writes ``.auto-agent/pr_review.json``."""

    from agent.lifecycle.standin import POStandin

    repo = await _load_repo(task.repo_id)
    standin = POStandin(task=task, repo=repo)
    await standin.review_pr(pr_diff, pr_metadata, {"workspace_root": workspace_root})


async def _load_repo(repo_id: int | None):
    """Fetch the ``Repo`` row for a task's ``repo_id``.

    Returns a minimal stub when the row is missing so the standin's
    fallback paths still fire (instead of crashing). The standin never
    escapes to the user — missing repo data is just another "no
    grounding context" case.
    """

    if repo_id is None:
        return _MinimalRepo(id=None, product_brief=None, mode=None)
    async with async_session() as s:
        row = (await s.execute(select(Repo).where(Repo.id == repo_id))).scalar_one_or_none()
    return row or _MinimalRepo(id=repo_id, product_brief=None, mode=None)


class _MinimalRepo:
    """Tiny fall-back shim — exposes the fields the standin reads."""

    def __init__(
        self,
        *,
        id: int | None,  # noqa: A002 — mirrors ORM column name; standin reads .id
        product_brief: str | None,
        mode: str | None,
    ) -> None:
        self.id = id
        self.product_brief = product_brief
        self.mode = mode
