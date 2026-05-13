"""Architect agent for the trio lifecycle.

Four phases: initial, consult, checkpoint, revision. Each persists an
``ArchitectAttempt`` row scoped to the trio parent task.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Literal

import structlog
from sqlalchemy import func, select

from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.trio.prompts import (
    ARCHITECT_CHECKPOINT_SYSTEM,
    ARCHITECT_CONSULT_SYSTEM,
    ARCHITECT_INITIAL_SYSTEM,
)
from shared.database import async_session
from shared.models import ArchitectAttempt, ArchitectPhase, Task, TaskStatus

log = structlog.get_logger()


_SYSTEM_PROMPTS = {
    "initial":    ARCHITECT_INITIAL_SYSTEM,
    "consult":    ARCHITECT_CONSULT_SYSTEM,
    "checkpoint": ARCHITECT_CHECKPOINT_SYSTEM,
    "revision":   ARCHITECT_INITIAL_SYSTEM,  # same shape as initial
}


def create_architect_agent(
    workspace: str,
    task_id: int,
    task_description: str,
    phase: Literal["initial", "consult", "checkpoint", "revision"],
    repo_name: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
):
    """Build an AgentLoop configured for the architect.

    The architect always has:
    - web_search + fetch_url (outside grounding)
    - record_decision + request_market_brief (its bespoke tools)
    - Standard file/bash/git tools
    """
    agent = create_agent(
        workspace=workspace,
        task_id=task_id,
        task_description=task_description,
        with_web=True,
        max_turns=80,
        include_methodology=False,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    # Replace the default registry with one that adds the architect-only tools.
    from agent.tools import create_default_registry

    agent.tools = create_default_registry(
        with_web=True,
        with_architect_tools=True,
    )
    # Inject the phase-specific system prompt.
    agent.system_prompt_override = _SYSTEM_PROMPTS[phase]
    return agent


# ---------------------------------------------------------------------------
# run_initial — the architect's first pass over a trio parent task.
# ---------------------------------------------------------------------------

# Matches the trailing ```json { ... } ``` block the architect prompt asks for.
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)


def _extract_backlog(text: str) -> list[dict] | None:
    """Pull the ``{"backlog": [...]}`` JSON block out of the architect output.

    Returns the backlog list on success, ``None`` on any failure path:
    missing block, malformed JSON, no ``backlog`` key, item missing
    required fields. Items default to ``status="pending"`` when absent.
    """
    if not text:
        return None
    # Prefer the last JSON block — the architect's final pass may emit
    # multiple (e.g. an example illustration followed by the real one).
    matches = list(_JSON_BLOCK_RE.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        backlog = payload.get("backlog") if isinstance(payload, dict) else None
        if not isinstance(backlog, list) or not backlog:
            continue
        ok = True
        for item in backlog:
            if not isinstance(item, dict):
                ok = False
                break
            if not all(k in item for k in ("id", "title", "description")):
                ok = False
                break
            item.setdefault("status", "pending")
        if ok:
            return backlog
    return None


def _extract_clarification(text: str) -> str | None:
    """Extract the architect's clarification question if present.

    Looks for the LAST ```json fenced block in the message whose top-level
    shape is ``{"decision": {"action": "awaiting_clarification", "question": "..."}}``.
    Returns the question string, or None if no such block exists or it's
    malformed. Returns None for blocks with action != "awaiting_clarification"
    or with no question field — callers fall through to backlog extraction.
    """
    import json
    import re

    blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        decision = data.get("decision")
        if not isinstance(decision, dict):
            continue
        if decision.get("action") != "awaiting_clarification":
            continue
        question = decision.get("question")
        if isinstance(question, str) and question.strip():
            return question
    return None


async def _prepare_parent_workspace(parent: Task) -> str:
    """Prepare a workspace for the architect's initial pass.

    With a repo attached: clone via ``agent.workspace.clone_repo`` and
    check out branch ``trio/<parent_id>`` off the repo's default branch
    (creating it if necessary).

    Without a repo (cold-start tasks like "build a TODO app"): allocate
    an empty directory under the workspaces root and ``git init`` it so
    the architect can scaffold + commit. The integration branch is still
    created under ``trio/<parent_id>`` for symmetry with the repo path.
    """
    from agent import sh
    from agent.workspace import (
        _AGENT_GIT_EMAIL,
        _AGENT_GIT_NAME,
        WORKSPACES_DIR,
        clone_repo,
        create_branch,
    )

    branch = f"trio/{parent.id}"

    if parent.repo is not None:
        base_branch = parent.repo.default_branch or "main"
        workspace = await clone_repo(
            parent.repo.url,
            parent.id,
            base_branch,
            user_id=parent.created_by_user_id,
            organization_id=parent.organization_id,
        )
        await create_branch(workspace, branch)
        return workspace

    # Repo-less cold start — bootstrap an empty git workspace.
    workspace = os.path.join(
        WORKSPACES_DIR, str(parent.organization_id), f"task-{parent.id}",
    )
    os.makedirs(workspace, exist_ok=True)
    if not os.path.isdir(os.path.join(workspace, ".git")):
        await sh.run(["git", "init", "-q"], cwd=workspace, timeout=30)
        await sh.run(
            ["git", "config", "user.email", _AGENT_GIT_EMAIL],
            cwd=workspace, timeout=10,
        )
        await sh.run(
            ["git", "config", "user.name", _AGENT_GIT_NAME],
            cwd=workspace, timeout=10,
        )
        # Seed an empty commit so HEAD exists before we branch off it.
        await sh.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=workspace, timeout=10,
        )
    await create_branch(workspace, branch)
    return workspace


async def _commit_and_open_initial_pr(parent: Task, workspace: str) -> str:
    """Commit scaffold + ARCHITECTURE.md, open the init PR, auto-merge.

    Sub-branches off the parent's integration branch ``trio/<parent_id>``
    to a fresh ``trio/<parent_id>/init`` head, commits anything the
    architect left in the working tree, pushes both branches, opens a
    PR back to the integration branch via the gh CLI, and asks GitHub to
    auto-squash-merge it.

    Returns the merged commit SHA on the integration branch (best-effort;
    falls back to HEAD of the head branch if no remote is reachable).
    """
    from agent import sh
    from agent.workspace import (
        commit_pending_changes,
        push_branch,
    )

    integration_branch = f"trio/{parent.id}"
    head_branch = f"trio/{parent.id}/init"

    # Sub-branch off the integration branch we're already on.
    await sh.run(
        ["git", "checkout", "-B", head_branch],
        cwd=workspace, timeout=30,
    )

    # Commit any pending scaffold + ARCHITECTURE.md the architect produced.
    # ``commit_pending_changes`` is a no-op when the working tree is clean.
    await commit_pending_changes(
        workspace, parent.id, f"init: architecture + scaffold — {parent.title}",
    )

    # Try to push and open a PR. If there's no remote configured (cold-start
    # workspaces without a repo), fall back to HEAD locally so callers still
    # get a usable SHA without crashing.
    remote_check = await sh.run(
        ["git", "remote"], cwd=workspace, timeout=10,
    )
    has_remote = bool((remote_check.stdout or "").strip())

    if has_remote:
        try:
            await push_branch(workspace, head_branch)
            # Make sure the integration branch exists upstream too so the PR
            # has somewhere to land.
            await sh.run(
                ["git", "push", "-u", "origin", integration_branch],
                cwd=workspace, timeout=30,
            )

            from shared.github_auth import get_github_token

            gh_env = {
                "GH_TOKEN": await get_github_token(
                    user_id=parent.created_by_user_id,
                    organization_id=parent.organization_id,
                ),
            }
            create_res = await sh.run(
                [
                    "gh", "pr", "create",
                    "--base", integration_branch,
                    "--head", head_branch,
                    "--title", f"init: architecture + scaffold — {parent.title}",
                    "--body", (
                        "Initial architecture pass for trio parent "
                        f"#{parent.id}.\n\n"
                        "Contains ARCHITECTURE.md, any scaffold the architect "
                        "produced, and seeds the integration branch for the "
                        "builder cycle."
                    ),
                ],
                cwd=workspace, timeout=30, env=gh_env,
            )
            if create_res.failed:
                log.warning(
                    "architect.initial_pr_create_failed",
                    task_id=parent.id,
                    stderr=(create_res.stderr or "")[:500],
                )
            else:
                # Auto-squash-merge — GitHub completes it once required checks
                # pass (or immediately when there are none).
                await sh.run(
                    ["gh", "pr", "merge", "--auto", "--squash"],
                    cwd=workspace, timeout=30, env=gh_env,
                )
        except Exception as e:  # pragma: no cover — best-effort remote plumbing
            log.warning(
                "architect.initial_pr_push_or_merge_failed",
                task_id=parent.id, error=str(e),
            )

    # Resolve the SHA we'll record. Prefer the local HEAD of the head branch
    # — that's what we committed, regardless of whether the auto-merge
    # completes synchronously.
    rev = await sh.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, timeout=10,
    )
    sha = (rev.stdout or "").strip()
    return sha[:40] if sha else ""


def _result_output(result: Any) -> str:
    """Extract the text output from whatever ``agent.run`` returned.

    Production path: ``AgentResult`` with an ``.output`` attribute.
    Test mocks: a string, or a ``MagicMock`` configured with ``output=``.
    """
    if hasattr(result, "output"):
        return result.output or ""
    return str(result) if result is not None else ""


def _result_tool_calls(result: Any, agent: Any) -> list:
    """Best-effort tool-call log for the ``ArchitectAttempt`` row.

    Looks first at the agent itself (mocks set ``tool_call_log``), then
    at the run result (``AgentResult.api_messages`` and friends), then
    falls back to an empty list.
    """
    for src in (agent, result):
        log_attr = getattr(src, "tool_call_log", None)
        if isinstance(log_attr, list):
            return log_attr
    calls = getattr(result, "tool_calls", None)
    if isinstance(calls, list):
        return calls
    return []


async def _emit_clarification(
    *,
    parent_task_id: int,
    agent,  # AgentLoop
    workspace,
    output: str,
    tool_calls: list[dict],
    question: str,
    phase: ArchitectPhase,
) -> None:
    """Persist the AgentLoop session, write the question row, transition
    the parent to AWAITING_CLARIFICATION, publish *_NEEDED.

    Called from run_initial / checkpoint / run_revision when the architect
    output contains an awaiting_clarification JSON block.
    """
    from agent.session import Session
    from orchestrator.state_machine import transition
    from shared.events import Event, TaskEventType, publish

    # 1. Persist the AgentLoop messages + api_messages so resume() can
    #    pick up exactly where the architect left off.
    session_id = f"trio-{parent_task_id}"
    session_blob_dir = workspace.root if hasattr(workspace, "root") else str(workspace)
    file_session = Session(session_id=session_id, storage_dir=session_blob_dir)
    await file_session.save(agent.messages, agent.api_messages)
    # session_blob_path is the file Session.save() wrote — relative path
    # under the workspace dir so the architect.resume() side can locate it
    # from any reconstructed workspace path.
    session_blob_path = f"{session_id}.json"

    # 2. Loop guard — count clarification rounds in this parent's lifetime.
    async with async_session() as s:
        prior = (
            await s.execute(
                select(func.count())
                .select_from(ArchitectAttempt)
                .where(ArchitectAttempt.task_id == parent_task_id)
                .where(ArchitectAttempt.clarification_question.is_not(None))
            )
        ).scalar_one()

        cap = int(os.environ.get("TRIO_MAX_CLARIFICATIONS", "3"))
        if prior >= cap:
            log.warning(
                "architect.clarification.loop_guard",
                task_id=parent_task_id, prior_rounds=prior, cap=cap,
            )
            parent = (
                await s.execute(select(Task).where(Task.id == parent_task_id))
            ).scalar_one()
            await transition(
                s, parent, TaskStatus.BLOCKED,
                message=f"architect asked for clarification {prior + 1}x; capped at {cap}",
            )
            s.add(ArchitectAttempt(
                task_id=parent_task_id,
                phase=phase,
                cycle=_next_cycle_sync(prior + 1),
                reasoning=output,
                tool_calls=tool_calls,
                clarification_question=question,
                session_blob_path=session_blob_path,
                decision={"action": "blocked",
                          "reason": "clarification loop guard"},
            ))
            await s.commit()
            return

        # 3. Normal path: write the attempt row + transition + publish.
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        s.add(ArchitectAttempt(
            task_id=parent_task_id,
            phase=phase,
            cycle=prior + 1,
            reasoning=output,
            tool_calls=tool_calls,
            clarification_question=question,
            session_blob_path=session_blob_path,
            decision={"action": "awaiting_clarification"},
        ))
        await transition(
            s, parent, TaskStatus.AWAITING_CLARIFICATION,
            message="Architect needs answers",
        )
        await s.commit()

    await publish(Event(
        type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
        task_id=parent_task_id,
        payload={"question": question},
    ))
    log.info(
        "architect.clarification.emitted",
        task_id=parent_task_id, round=prior + 1, question_preview=question[:120],
    )


def _next_cycle_sync(n: int) -> int:
    """Small helper for tests / clarity. Cycles are 1-indexed."""
    return n


async def run_initial(parent_task_id: int) -> None:
    """Run the architect's initial pass on a trio parent task.

    On success: writes the integration commit SHA + backlog to the parent,
    persists an ``ArchitectAttempt`` row (``phase=INITIAL``, ``cycle=1``).
    On JSON-extraction failure: marks the parent ``BLOCKED`` and persists
    a blocked attempt row instead.
    """
    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        # Pre-resolve everything we'll need outside the session — the
        # architect run is long, and we don't want a stale connection.
        task_description = parent.description or parent.title
        task_title = parent.title
        repo_name = parent.repo.name if parent.repo else None
        org_id = parent.organization_id
        home_dir = await home_dir_for_task(parent)

    workspace = await _prepare_parent_workspace(parent)

    agent = create_architect_agent(
        workspace=workspace,
        task_id=parent_task_id,
        task_description=task_description,
        phase="initial",
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    run_result = await agent.run(
        f"Run the initial architecture pass for: {task_title}\n\n"
        f"Task description:\n{task_description}",
    )
    output = _result_output(run_result)
    tool_calls = _result_tool_calls(run_result, agent)

    # First: did the architect ask for clarification? If so, persist
    # session + transition state instead of trying to parse a backlog.
    clarification = _extract_clarification(output)
    if clarification is not None:
        await _emit_clarification(
            parent_task_id=parent_task_id,
            agent=agent,
            workspace=workspace,
            output=output,
            tool_calls=tool_calls,
            question=clarification,
            phase=ArchitectPhase.INITIAL,
        )
        return

    backlog = _extract_backlog(output)

    if backlog is None:
        log.error(
            "architect.run_initial.invalid_json",
            task_id=parent_task_id,
            output_preview=output[:300],
        )
        async with async_session() as s:
            parent = (
                await s.execute(select(Task).where(Task.id == parent_task_id))
            ).scalar_one()
            # Best-effort transition — if the parent isn't in a status that
            # allows BLOCKED, fall back to writing the attempt row only.
            try:
                from orchestrator.state_machine import transition

                await transition(
                    s, parent, TaskStatus.BLOCKED,
                    message="architect.run_initial: invalid JSON backlog",
                )
            except Exception:
                parent.status = TaskStatus.BLOCKED
            s.add(ArchitectAttempt(
                task_id=parent.id,
                phase=ArchitectPhase.INITIAL,
                cycle=1,
                reasoning=output,
                decision={
                    "action": "blocked",
                    "reason": "invalid JSON from architect",
                },
                tool_calls=tool_calls,
            ))
            await s.commit()
        return

    commit_sha = await _commit_and_open_initial_pr(parent, workspace)

    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        parent.trio_backlog = backlog
        s.add(ArchitectAttempt(
            task_id=parent.id,
            phase=ArchitectPhase.INITIAL,
            cycle=1,
            reasoning=output,
            commit_sha=commit_sha or None,
            tool_calls=tool_calls,
        ))
        await s.commit()

    log.info(
        "architect.run_initial.complete",
        task_id=parent_task_id,
        backlog_size=len(backlog),
        commit_sha=commit_sha,
    )


# ---------------------------------------------------------------------------
# consult — the builder asks the architect a mid-build question.
# ---------------------------------------------------------------------------

# Matches the trailing ```json { ... } ``` block the consult prompt asks for.
_CONSULT_JSON_RE = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)


def _extract_consult_payload(text: str) -> dict | None:
    """Pull ``{"answer": "...", "architecture_md_updated": bool}`` out of output.

    Returns ``None`` on any failure path (missing block, malformed JSON,
    missing ``answer`` key). Defaults ``architecture_md_updated`` to
    ``False`` when absent so callers can rely on it being present.
    """
    if not text:
        return None
    matches = list(_CONSULT_JSON_RE.finditer(text))
    if not matches:
        return None
    # Prefer the last valid block in case the architect emits multiple.
    for m in reversed(matches):
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "answer" not in payload:
            continue
        payload.setdefault("architecture_md_updated", False)
        return payload
    return None


async def _commit_consult_doc_update(parent: Task, workspace: str) -> str:
    """Commit ARCHITECTURE.md changes on a one-off consult sub-branch.

    Mirrors ``_commit_and_open_initial_pr`` but targets a timestamped
    sub-branch ``trio/<parent_id>/consult-<unix_ts>`` so multiple consult
    cycles don't collide. Pushes, opens a PR back to the integration
    branch, and asks GitHub to auto-squash-merge it.

    Returns the local HEAD SHA of the head branch — the auto-merge may
    complete asynchronously, so we record what we committed rather than
    blocking on the squash.
    """
    import time

    from agent import sh
    from agent.workspace import commit_pending_changes, push_branch

    integration_branch = f"trio/{parent.id}"
    head_branch = f"trio/{parent.id}/consult-{int(time.time())}"

    # Sub-branch off whatever HEAD currently is (the integration branch).
    await sh.run(
        ["git", "checkout", "-B", head_branch],
        cwd=workspace, timeout=30,
    )

    await commit_pending_changes(
        workspace, parent.id, f"consult: update ARCHITECTURE.md — {parent.title}",
    )

    remote_check = await sh.run(
        ["git", "remote"], cwd=workspace, timeout=10,
    )
    has_remote = bool((remote_check.stdout or "").strip())

    if has_remote:
        try:
            await push_branch(workspace, head_branch)
            await sh.run(
                ["git", "push", "-u", "origin", integration_branch],
                cwd=workspace, timeout=30,
            )

            from shared.github_auth import get_github_token

            gh_env = {
                "GH_TOKEN": await get_github_token(
                    user_id=parent.created_by_user_id,
                    organization_id=parent.organization_id,
                ),
            }
            create_res = await sh.run(
                [
                    "gh", "pr", "create",
                    "--base", integration_branch,
                    "--head", head_branch,
                    "--title", f"consult: update ARCHITECTURE.md — {parent.title}",
                    "--body", (
                        "Architecture clarification from mid-build consult for "
                        f"trio parent #{parent.id}.\n\n"
                        "Updates ARCHITECTURE.md in response to a builder "
                        "question; merges back to the integration branch so "
                        "subsequent builder turns see the new guidance."
                    ),
                ],
                cwd=workspace, timeout=30, env=gh_env,
            )
            if create_res.failed:
                log.warning(
                    "architect.consult_pr_create_failed",
                    task_id=parent.id,
                    stderr=(create_res.stderr or "")[:500],
                )
            else:
                await sh.run(
                    ["gh", "pr", "merge", "--auto", "--squash"],
                    cwd=workspace, timeout=30, env=gh_env,
                )
        except Exception as e:  # pragma: no cover — best-effort remote plumbing
            log.warning(
                "architect.consult_pr_push_or_merge_failed",
                task_id=parent.id, error=str(e),
            )

    rev = await sh.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, timeout=10,
    )
    sha = (rev.stdout or "").strip()
    return sha[:40] if sha else ""


async def consult(
    *,
    parent_task_id: int,
    child_task_id: int,
    question: str,
    why: str,
) -> dict:
    """Answer a builder's mid-build question.

    Loads the parent task, prepares its workspace, runs the architect
    in ``consult`` mode with the builder's question + rationale folded
    into the task description, and parses the resulting JSON payload.

    If ``architecture_md_updated`` is true, commits the doc change on a
    timestamped sub-branch and opens a PR back to the integration
    branch (``_commit_consult_doc_update``).

    Persists an ``ArchitectAttempt`` row with ``phase=CONSULT``,
    monotonically-increasing ``cycle`` (per parent), the question/why,
    and ``commit_sha`` when a doc PR was opened.

    Returns ``{"answer": str, "architecture_md_updated": bool}``.
    """
    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        # Pre-resolve everything we'll need outside the session.
        task_description = parent.description or parent.title
        repo_name = parent.repo.name if parent.repo else None
        org_id = parent.organization_id
        home_dir = await home_dir_for_task(parent)

    workspace = await _prepare_parent_workspace(parent)

    consult_description = (
        f"{task_description}\n\n"
        f"[Consult question from builder #{child_task_id}]: {question}\n"
        f"[Why]: {why}"
    )

    agent = create_architect_agent(
        workspace=workspace,
        task_id=parent_task_id,
        task_description=consult_description,
        phase="consult",
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    run_result = await agent.run(consult_description)
    output = _result_output(run_result)
    tool_calls = _result_tool_calls(run_result, agent)

    payload = _extract_consult_payload(output) or {
        "answer": output.strip(),
        "architecture_md_updated": False,
    }

    commit_sha: str | None = None
    if payload["architecture_md_updated"]:
        commit_sha = await _commit_consult_doc_update(parent, workspace)

    async with async_session() as s:
        existing = (
            await s.execute(
                select(ArchitectAttempt).where(
                    (ArchitectAttempt.task_id == parent_task_id)
                    & (ArchitectAttempt.phase == ArchitectPhase.CONSULT)
                )
            )
        ).scalars().all()
        cycle = len(existing) + 1

        s.add(ArchitectAttempt(
            task_id=parent_task_id,
            phase=ArchitectPhase.CONSULT,
            cycle=cycle,
            reasoning=output,
            consult_question=question,
            consult_why=why,
            commit_sha=commit_sha or None,
            tool_calls=tool_calls,
        ))
        await s.commit()

    log.info(
        "architect.consult.complete",
        parent_task_id=parent_task_id,
        child_task_id=child_task_id,
        cycle=cycle,
        architecture_md_updated=payload["architecture_md_updated"],
        commit_sha=commit_sha,
    )

    return {
        "answer": payload["answer"],
        "architecture_md_updated": bool(payload["architecture_md_updated"]),
    }


# ---------------------------------------------------------------------------
# checkpoint — architect reviews progress after a child merges (or CI fails).
# ---------------------------------------------------------------------------

# Matches the trailing ```json { ... } ``` block the checkpoint prompt asks for.
_CHECKPOINT_JSON_RE = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)


def _extract_checkpoint_payload(text: str) -> dict | None:
    """Pull ``{"backlog": [...], "decision": {...}}`` from a ```json``` block.

    Returns ``None`` on any failure path: missing block, malformed JSON,
    missing ``backlog`` or ``decision`` keys, ``backlog`` not a list,
    ``decision`` not a dict, or ``decision.action`` absent. Prefers the
    last valid block when the architect emits multiple.
    """
    if not text:
        return None
    matches = list(_CHECKPOINT_JSON_RE.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        backlog = payload.get("backlog")
        decision = payload.get("decision")
        if not isinstance(backlog, list):
            continue
        if not isinstance(decision, dict) or "action" not in decision:
            continue
        return {"backlog": backlog, "decision": decision}
    return None


async def _next_cycle(session, parent_id: int, phase: ArchitectPhase) -> int:
    """Return the next ``cycle`` number for ``(parent_id, phase)``.

    Counts existing ``ArchitectAttempt`` rows for the pair and adds one.
    Used by ``checkpoint`` and ``run_revision`` (and could be used by
    ``consult`` — kept inline there to avoid breaking its tests).
    """
    existing = (
        await session.execute(
            select(ArchitectAttempt).where(
                (ArchitectAttempt.task_id == parent_id)
                & (ArchitectAttempt.phase == phase)
            )
        )
    ).scalars().all()
    return len(existing) + 1


async def checkpoint(
    parent_task_id: int,
    *,
    child_task_id: int | None = None,
    repair_context: dict | None = None,
) -> dict:
    """Run an architect checkpoint pass over the parent task.

    Two flavors share the same code path, distinguished by which kwarg
    the caller supplies:

    - ``child_task_id`` — a child task just merged; the architect reviews
      the diff and decides whether the backlog needs to change.
    - ``repair_context`` — the integration PR failed CI; the architect
      diagnoses and adds fix work items to the backlog.

    Returns the decision dict (matches ``shared.types.ArchitectDecision``
    shape). On JSON-extraction failure, returns a ``blocked`` decision
    and persists an attempt row with that decision; the parent's backlog
    is left untouched in that case.
    """
    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        task_description = parent.description or parent.title
        task_title = parent.title
        repo_name = parent.repo.name if parent.repo else None
        org_id = parent.organization_id
        home_dir = await home_dir_for_task(parent)

    workspace = await _prepare_parent_workspace(parent)

    # Build the prompt suffix from whichever kwarg the caller supplied.
    suffix = ""
    if child_task_id is not None:
        suffix = (
            f"\n\nChild task #{child_task_id} just merged. "
            "Review its diff via git log/git diff."
        )
    elif repair_context is not None:
        ci_log = str(repair_context.get("ci_log", ""))[:4000]
        failed_pr_url = repair_context.get("failed_pr_url", "")
        suffix = (
            f"\n\nThe integration PR ({failed_pr_url}) failed CI: "
            f"{ci_log}. Diagnose and add fix work items."
        )

    checkpoint_description = f"{task_description}{suffix}"

    agent = create_architect_agent(
        workspace=workspace,
        task_id=parent_task_id,
        task_description=checkpoint_description,
        phase="checkpoint",
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    run_result = await agent.run(
        f"Run a checkpoint review for: {task_title}\n\n"
        f"Task description:\n{checkpoint_description}",
    )
    output = _result_output(run_result)
    tool_calls = _result_tool_calls(run_result, agent)

    payload = _extract_checkpoint_payload(output)

    if payload is None:
        log.error(
            "architect.checkpoint.invalid_json",
            task_id=parent_task_id,
            output_preview=output[:300],
        )
        blocked = {"action": "blocked", "reason": "invalid checkpoint JSON"}
        async with async_session() as s:
            cycle = await _next_cycle(s, parent_task_id, ArchitectPhase.CHECKPOINT)
            s.add(ArchitectAttempt(
                task_id=parent_task_id,
                phase=ArchitectPhase.CHECKPOINT,
                cycle=cycle,
                reasoning=output or "",
                decision=blocked,
                tool_calls=tool_calls,
            ))
            await s.commit()
        return blocked

    decision = payload["decision"]
    backlog = payload["backlog"]

    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        parent.trio_backlog = backlog
        cycle = await _next_cycle(s, parent_task_id, ArchitectPhase.CHECKPOINT)
        s.add(ArchitectAttempt(
            task_id=parent_task_id,
            phase=ArchitectPhase.CHECKPOINT,
            cycle=cycle,
            reasoning=output,
            decision=decision,
            tool_calls=tool_calls,
        ))
        await s.commit()

    log.info(
        "architect.checkpoint.complete",
        task_id=parent_task_id,
        child_task_id=child_task_id,
        has_repair_context=repair_context is not None,
        cycle=cycle,
        decision_action=decision.get("action"),
        backlog_size=len(backlog),
    )

    return decision


# ---------------------------------------------------------------------------
# run_revision — architect re-thinks the design and rewrites ARCHITECTURE.md.
# ---------------------------------------------------------------------------


async def run_revision(parent_task_id: int) -> None:
    """Run a revision pass on a trio parent task.

    Same overall shape as ``run_initial`` but with the revision system
    prompt and a description suffix telling the architect this is a
    re-pass. On success: writes the new commit SHA + rewritten backlog
    to the parent and persists an ``ArchitectAttempt`` row with
    ``phase=REVISION`` and a fresh ``cycle``. On JSON-extraction
    failure: transitions the parent to ``BLOCKED`` and persists a
    blocked revision row.
    """
    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        task_description = parent.description or parent.title
        task_title = parent.title
        repo_name = parent.repo.name if parent.repo else None
        org_id = parent.organization_id
        home_dir = await home_dir_for_task(parent)

    workspace = await _prepare_parent_workspace(parent)

    revision_description = (
        f"{task_description}\n\n"
        "[Revision pass — design changed. Rewrite ARCHITECTURE.md.]"
    )

    agent = create_architect_agent(
        workspace=workspace,
        task_id=parent_task_id,
        task_description=revision_description,
        phase="revision",
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    run_result = await agent.run(
        f"Run a revision pass for: {task_title}\n\n"
        f"Task description:\n{revision_description}",
    )
    output = _result_output(run_result)
    tool_calls = _result_tool_calls(run_result, agent)

    backlog = _extract_backlog(output)

    if backlog is None:
        log.error(
            "architect.run_revision.invalid_json",
            task_id=parent_task_id,
            output_preview=output[:300],
        )
        async with async_session() as s:
            parent = (
                await s.execute(select(Task).where(Task.id == parent_task_id))
            ).scalar_one()
            try:
                from orchestrator.state_machine import transition

                await transition(
                    s, parent, TaskStatus.BLOCKED,
                    message="architect.run_revision: invalid JSON backlog",
                )
            except Exception:
                parent.status = TaskStatus.BLOCKED
            cycle = await _next_cycle(s, parent_task_id, ArchitectPhase.REVISION)
            s.add(ArchitectAttempt(
                task_id=parent.id,
                phase=ArchitectPhase.REVISION,
                cycle=cycle,
                reasoning=output or "",
                decision={
                    "action": "blocked",
                    "reason": "invalid JSON from architect",
                },
                tool_calls=tool_calls,
            ))
            await s.commit()
        return

    commit_sha = await _commit_and_open_initial_pr(parent, workspace)

    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        parent.trio_backlog = backlog
        cycle = await _next_cycle(s, parent_task_id, ArchitectPhase.REVISION)
        s.add(ArchitectAttempt(
            task_id=parent.id,
            phase=ArchitectPhase.REVISION,
            cycle=cycle,
            reasoning=output,
            commit_sha=commit_sha or None,
            tool_calls=tool_calls,
        ))
        await s.commit()

    log.info(
        "architect.run_revision.complete",
        task_id=parent_task_id,
        backlog_size=len(backlog),
        commit_sha=commit_sha,
        cycle=cycle,
    )
