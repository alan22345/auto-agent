"""Coding phase — implement the plan, self-review, push, and create a PR.

Two paths inside ``handle_coding``:
  - ``_handle_coding_single`` — standard one-shot implementation pass.
  - ``_handle_coding_with_subtasks`` — complex_large path: each plan phase
    runs as a fresh-context subtask (no session resume, intentional
    isolation per the superpowers pattern).

After implementation both paths converge on ``_finish_coding`` (self-review
loop, auto-commit safety net, push, PR creation, hand-off to independent
review).
"""

from __future__ import annotations

import re as _re

import httpx

from agent.lifecycle import review
from agent.lifecycle._clarification import _extract_clarification
from agent.lifecycle._naming import _branch_name, _fresh_session_id, _pr_title, _session_id
from agent.lifecycle._orchestrator_api import (
    ORCHESTRATOR_URL,
    get_freeform_config,
    get_repo,
    get_task,
    transition_task,
)
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.intent import extract_intent
from agent.prompts import (
    MEMORY_REFLECTION_PROMPT,
    build_coding_prompt,
    build_review_prompt,
)
from agent.tools import dev_server as _dev_server
from agent.workspace import (
    cleanup_workspace,
    clone_repo,
    commit_pending_changes,
    create_branch,
    ensure_branch_has_commits,
    push_branch,
)
from shared.events import (
    Event,
    coding_server_boot_failed,
    publish,
    repo_onboard,
    task_clarification_needed,
    task_subtask_progress,
)
from shared.logging import setup_logging
from shared.quotas import QuotaExceeded

log = setup_logging("agent.lifecycle.coding")


MAX_REVIEW_RETRIES = 2

# Marker the self-review prompt instructs the agent to emit on a line by
# itself when no issues remain. Substring matching is unsafe because the
# agent often *mentions* the marker (e.g. "Outputting `REVIEW_PASSED`
# would be misleading…") — that false-positived task #156 to "passed".
_REVIEW_PASSED_RE = _re.compile(r"^\s*REVIEW_PASSED\s*$", _re.MULTILINE)


def _is_cli_error(output: str) -> bool:
    """True if `output` is a Claude CLI error sentinel rather than a real response.

    The Claude CLI provider returns "[ERROR] CLI exited N: …" or
    "[ERROR] Claude Code CLI timed out" as the agent output when the
    subprocess fails. The lifecycle previously treated those as normal
    completions and marched onward — see task #156 post-mortem.
    """
    return bool(output) and output.lstrip().startswith("[ERROR]")


def _review_passed(output: str) -> bool:
    """True iff the agent emitted REVIEW_PASSED on its own line."""
    return bool(output) and _REVIEW_PASSED_RE.search(output) is not None


def _parse_plan_phases(plan: str) -> list[dict]:
    """Parse a plan into phases by splitting on '## Phase N' headers."""
    phase_pattern = _re.compile(r"^##\s+Phase\s+\d+", _re.MULTILINE)
    splits = list(phase_pattern.finditer(plan))
    if len(splits) < 2:
        return []
    phases = []
    for i, match in enumerate(splits):
        start = match.start()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(plan)
        chunk = plan[start:end].strip()
        first_line = chunk.split("\n", 1)[0]
        title = first_line.lstrip("#").strip()
        phases.append({"title": title, "content": chunk, "status": "pending", "output_preview": ""})
    return phases


async def _update_subtasks(task_id: int, subtasks: list[dict], current: int | None) -> None:
    api_subtasks = [
        {
            "title": s["title"],
            "status": s["status"],
            "output_preview": s.get("output_preview", ""),
        }
        for s in subtasks
    ]
    async with httpx.AsyncClient() as client:
        await client.patch(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/subtasks",
            json={"subtasks": api_subtasks, "current_subtask": current},
        )


async def _is_trio_child(task) -> bool:
    """True when this task is a trio child (parent task is in TRIO_EXECUTING).

    A trio child is a normal coding task with ``parent_task_id`` pointing at
    a parent currently driving the trio loop. We re-check the parent's
    status against the live DB (not whatever stale snapshot was passed in)
    because the parent's phase can advance between the child being queued
    and the child reaching the coding handler.
    """
    if not getattr(task, "parent_task_id", None):
        return False
    from sqlalchemy import select

    from shared.database import async_session
    from shared.models import Task, TaskStatus

    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == task.parent_task_id))
        ).scalar_one_or_none()
    return parent is not None and parent.status == TaskStatus.TRIO_EXECUTING


async def _build_trio_child_prompt(*, child_description: str, workspace: str) -> str:
    """Assemble the trio-child augmentation appended to the coding prompt.

    Inputs are plain strings so this helper is trivially unit-testable —
    no DB session, no TaskData required.
    """
    import os

    arch_path = os.path.join(workspace, "ARCHITECTURE.md")
    if os.path.isfile(arch_path):
        try:
            with open(arch_path) as f:
                arch = f.read()
        except OSError:
            arch = "(ARCHITECTURE.md unreadable)"
    else:
        arch = "(ARCHITECTURE.md not found in workspace)"

    return (
        "You are a trio builder. Your task is one bounded work item.\n\n"
        f"== Work item ==\n{child_description}\n\n"
        f"== ARCHITECTURE.md ==\n{arch}\n\n"
        "If you hit an ambiguity that touches design (file layout, data model, "
        "abstraction choice), call `consult_architect(question, why)`. For "
        "code-local decisions, decide yourself.\n"
    )


async def _maybe_start_coding_server(task, workspace: str):
    """Return a dev-server async context manager when applicable, else None.

    Started when task.affected_routes is non-empty AND a run command resolves.
    Caller owns the context-manager lifecycle.
    """
    routes = getattr(task, "affected_routes", None) or []
    if not routes:
        return None
    override = None
    if getattr(task, "freeform_mode", False) and getattr(task, "repo_name", None):
        cfg = await get_freeform_config(task.repo_name)
        if cfg and getattr(cfg, "run_command", None):
            override = cfg.run_command
    if _dev_server.sniff_run_command(workspace, override=override) is None:
        return None
    return _dev_server.start_dev_server(workspace, override=override)


async def handle_coding(task_id: int, retry_reason: str | None = None) -> None:
    """Run the agent to implement, self-review, test, and create a PR."""
    task = await get_task(task_id)
    if not task:
        return

    if not task.repo_name:
        await transition_task(task_id, "blocked", "No repo assigned to this task")
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        await transition_task(task_id, "blocked", f"Repo '{task.repo_name}' not found")
        return

    if not repo.harness_onboarded:
        log.info(f"Repo '{repo.name}' not harness-onboarded, triggering onboarding")
        await publish(repo_onboard(repo_id=repo.id, repo_name=repo.name))

    session_id = _session_id(task_id, task.created_at)
    base_branch = repo.default_branch
    fallback_branch: str | None = None

    # If the repo has a dev branch configured, ALL tasks target it.
    # Code deploys to dev after CI passes; promotion to prod is manual.
    if task.repo_name:
        freeform_cfg = await get_freeform_config(task.repo_name)
        if freeform_cfg and freeform_cfg.dev_branch:
            base_branch = freeform_cfg.dev_branch
            fallback_branch = freeform_cfg.prod_branch or repo.default_branch
            log.info(f"Targeting dev branch '{base_branch}' for task #{task_id}")

    is_continuation = task.plan is not None or retry_reason is not None
    log.info(
        f"Coding task #{task_id} in {task.repo_name} "
        f"(session={session_id}, resume={is_continuation})"
    )
    workspace = await clone_repo(
        repo.url, task_id, base_branch,
        fallback_branch=fallback_branch,
        user_id=task.created_by_user_id,
        organization_id=task.organization_id,
    )

    # Reuse existing branch or generate new one
    if task.branch_name:
        branch_name = task.branch_name
    else:
        branch_name = await _branch_name(task_id, task.title)
        async with httpx.AsyncClient() as client:
            await client.patch(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/branch",
                json={"branch_name": branch_name},
            )

    await create_branch(workspace, branch_name)

    # Extract structured intent (fast LLM call — non-blocking on failure)
    intent = await extract_intent(task.title, task.description)
    if intent:
        log.info(f"Intent extracted for task #{task_id}: {intent.get('change_type', '?')}")

    try:
        phases = []
        if task.complexity == "complex_large" and task.plan and not retry_reason:
            phases = _parse_plan_phases(task.plan)

        if phases and len(phases) >= 2:
            await _handle_coding_with_subtasks(
                task_id,
                task,
                phases,
                workspace,
                session_id,
                base_branch,
                branch_name,
                is_continuation,
                repo,
                intent=intent,
            )
        else:
            await _handle_coding_single(
                task_id,
                task,
                workspace,
                session_id,
                base_branch,
                branch_name,
                is_continuation,
                repo,
                retry_reason,
                intent=intent,
            )
    except QuotaExceeded as e:
        log.info("task_blocked_on_quota", task_id=task_id, reason=str(e))
        await transition_task(task_id, "blocked_on_quota", str(e))
        cleanup_workspace(task_id, organization_id=task.organization_id)
    except Exception as e:
        log.exception(f"Coding failed for task #{task_id}")
        await transition_task(task_id, "failed", str(e))
        cleanup_workspace(task_id, organization_id=task.organization_id)


async def _handle_coding_single(
    task_id: int,
    task,
    workspace: str,
    session_id: str,
    base_branch: str,
    branch_name: str,
    is_continuation: bool,
    repo,
    retry_reason: str | None = None,
    intent: dict | None = None,
) -> None:
    """Standard coding path — single implementation pass."""
    from agent.prompts import augment_coding_prompt_with_server

    coding_prompt = build_coding_prompt(
        task.title, task.description, task.plan, repo.summary, repo.ci_checks, intent=intent
    )
    if retry_reason:
        coding_prompt += (
            f"\n\nPrevious attempt failed. Reason: {retry_reason}\nFix the issues and try again."
        )

    server_cm = await _maybe_start_coding_server(task, workspace)
    server_handle = None
    try:
        if server_cm is not None:
            try:
                server_handle = await server_cm.__aenter__()
                await _dev_server.wait_for_port(
                    server_handle.port, timeout=60, log_path=server_handle.log_path,
                )
                coding_prompt = augment_coding_prompt_with_server(
                    coding_prompt,
                    port=server_handle.port,
                    affected_routes=getattr(task, "affected_routes", None) or [],
                )
            except (_dev_server.BootTimeout, _dev_server.BootError) as e:
                await publish(coding_server_boot_failed(task_id, str(e)))
                import contextlib
                with contextlib.suppress(Exception):
                    await server_cm.__aexit__(type(e), e, None)
                server_handle = None
                server_cm = None

        # Trio children get ARCHITECTURE.md + consult_architect grafted on.
        # Non-trio tasks take exactly today's path.
        trio_child = await _is_trio_child(task)
        if trio_child:
            coding_prompt += "\n\n" + await _build_trio_child_prompt(
                child_description=task.description,
                workspace=workspace,
            )

        agent = create_agent(
            workspace,
            session_id=session_id,
            max_turns=50,
            task_id=task_id,
            task_description=task.description,
            repo_name=repo.name,
            complexity=task.complexity,
            home_dir=await home_dir_for_task(task),
            org_id=task.organization_id,
            with_browser=server_handle is not None,
            with_consult_architect=trio_child,
            dev_server_log_path=server_handle.log_path if server_handle else None,
        )
        result = await agent.run(coding_prompt, resume=is_continuation)
    finally:
        if server_cm is not None and server_handle is not None:
            try:
                await server_cm.__aexit__(None, None, None)
            except Exception:
                log.exception("dev server cleanup raised")

    output = result.output
    log.info(f"Coding output for task #{task_id}: {output[:300]}...")

    if _is_cli_error(output):
        raise RuntimeError(f"agent CLI error during coding: {output.strip()[:300]}")

    # Check for clarification
    question = _extract_clarification(output)
    if question:
        log.info(f"Task #{task_id} needs clarification: {question[:100]}...")
        await transition_task(task_id, "awaiting_clarification", question)
        await publish(
            task_clarification_needed(task_id, question=question, phase="coding")
        )
        return

    # Post-task memory reflection — agent writes learnings into the graph
    try:
        reflection_agent = create_agent(
            workspace, session_id=session_id, max_turns=5, task_id=task_id,
            home_dir=await home_dir_for_task(task),
            org_id=task.organization_id,
        )
        await reflection_agent.run(MEMORY_REFLECTION_PROMPT, resume=True)
        log.info(f"Task #{task_id}: memory reflection complete")
    except Exception:
        log.warning(f"Task #{task_id}: memory reflection failed (non-fatal)")

    await _finish_coding(task_id, task, workspace, session_id, base_branch, branch_name)


async def _handle_coding_with_subtasks(
    task_id: int,
    task,
    phases: list[dict],
    workspace: str,
    session_id: str,
    base_branch: str,
    branch_name: str,
    is_continuation: bool,
    repo,
    intent: dict | None = None,
) -> None:
    """Complex-large coding path — implement each phase as a subtask."""
    total = len(phases)

    # Resume from existing subtask state if available
    existing = task.subtasks or []
    if existing and len(existing) == total:
        done_count = sum(1 for s in existing if s.get("status") == "done")
        if done_count == total:
            log.info(f"Task #{task_id}: all {total} subtasks already done, skipping to review + PR")
            await _finish_coding(task_id, task, workspace, session_id, base_branch, branch_name)
            return
        for i, ex in enumerate(existing):
            if ex.get("status") == "done":
                phases[i]["status"] = "done"
                phases[i]["output_preview"] = ex.get("output_preview", "")
        start_from = done_count
        log.info(f"Task #{task_id}: resuming complex-large from subtask {start_from + 1}/{total}")
    else:
        start_from = 0
        log.info(f"Task #{task_id}: complex-large with {total} subtasks")

    await _update_subtasks(task_id, phases, start_from)

    for i, phase in enumerate(phases):
        if phase["status"] == "done":
            continue
        phases[i]["status"] = "running"
        await _update_subtasks(task_id, phases, i)

        await publish(
            task_subtask_progress(
                task_id,
                current=i + 1,
                total=total,
                title=phase["title"],
                status="running",
            )
        )

        from agent.prompts import _intent_section

        intent_block = _intent_section(intent)
        intent_text = f"\n{intent_block}\n\n" if intent_block else ""
        prompt = (
            f"You are implementing a large task in phases. This is phase {i + 1} of {total}.\n\n"
            f"## Overall task\n{task.title}\n\n{task.description}\n"
            f"{intent_text}"
            f"## Current phase to implement\n{phase['content']}\n\n"
        )
        if i == 0:
            prompt += (
                f"## Full plan for context (implement ONLY the current phase above)\n{task.plan}\n\n"
                "Implement ONLY the current phase. Commit your changes before stopping.\n"
            )
        else:
            # Provide context about what previous phases did (fresh context pattern)
            prev_summaries = []
            for j in range(i):
                title = phases[j].get("title", f"Phase {j + 1}")
                preview = phases[j].get("output_preview", "completed")
                prev_summaries.append(f"### Phase {j + 1}: {title}\n{preview}")
            prev_context = "\n\n".join(prev_summaries)

            prompt += (
                f"## Previous phases (already implemented — do NOT redo)\n{prev_context}\n\n"
                "Run `git log --oneline -10` and `git diff --stat HEAD~5` to see what previous phases changed.\n\n"
                "Implement ONLY the current phase. Commit your changes before stopping.\n"
            )

        log.info(f"Task #{task_id}: starting subtask {i + 1}/{total} — {phase['title']}")
        # Fresh agent per subtask (context isolation — superpowers pattern).
        # Each subtask gets a fresh UUID session id; resume=False means the
        # CLI registers a new session rather than resuming. Must be a real
        # UUID — Claude CLI 2.1.x rejects suffixed strings like
        # "<uuid>-phase-1" with "Invalid session ID. Must be a valid UUID."
        # which silently failed task #156.
        subtask_session = _fresh_session_id(task_id, f"phase-{i + 1}")
        agent = create_agent(
            workspace, session_id=subtask_session, max_turns=40,
            home_dir=await home_dir_for_task(task),
            org_id=task.organization_id,
        )
        result = await agent.run(prompt, resume=False)
        output = result.output
        log.info(f"Task #{task_id} subtask {i + 1} output: {output[:300]}...")

        if _is_cli_error(output):
            phases[i]["status"] = "failed"
            phases[i]["output_preview"] = output[:1500]
            await _update_subtasks(task_id, phases, i)
            raise RuntimeError(
                f"subtask {i + 1}/{total} ({phase['title']}) failed: "
                f"agent CLI error: {output.strip()[:300]}"
            )

        question = _extract_clarification(output)
        if question:
            phases[i]["status"] = "blocked"
            await _update_subtasks(task_id, phases, i)
            await transition_task(task_id, "awaiting_clarification", question)
            await publish(
                task_clarification_needed(
                    task_id,
                    question=question,
                    phase=f"subtask {i + 1}: {phase['title']}",
                )
            )
            return

        phases[i]["status"] = "done"
        phases[i]["output_preview"] = output[:1500]
        await _update_subtasks(task_id, phases, i)

        await publish(
            task_subtask_progress(
                task_id,
                current=i + 1,
                total=total,
                title=phase["title"],
                status="done",
            )
        )

    log.info(f"Task #{task_id}: all {total} subtasks complete, proceeding to review + PR")
    await _finish_coding(task_id, task, workspace, session_id, base_branch, branch_name)


async def _open_pr_and_advance(
    task_id: int,
    task,
    workspace: str,
    base_branch: str,
    branch_name: str,
) -> None:
    """Push branch, create PR, kick off review. Idempotent — safe on retry."""
    committed_now = await commit_pending_changes(workspace, task_id, task.title)
    if committed_now:
        log.warning(
            f"Task #{task_id}: agent left uncommitted changes — auto-committed them before push"
        )
    await ensure_branch_has_commits(workspace, base_branch)
    await push_branch(workspace, branch_name)

    pr_body = (
        f"## Auto-Agent Task #{task_id}\n\n"
        f"**Task:** {task.title}\n\n"
        f"**Description:** {task.description[:500]}\n\n"
        f"---\n"
        f"*Generated by auto-agent. Code was self-reviewed for correctness, security, and root-cause analysis.*"
    )
    title = await _pr_title(task.title)
    pr_url = await review.create_pr(
        workspace, title, pr_body, base_branch, branch_name,
        user_id=task.created_by_user_id,
        organization_id=task.organization_id,
    )
    log.info(f"PR created: {pr_url}")
    if not pr_url.startswith("http"):
        raise RuntimeError(f"gh pr create returned invalid URL: {pr_url!r}")

    await review.handle_independent_review(task_id, pr_url, branch_name)


async def _finish_coding(
    task_id: int,
    task,
    workspace: str,
    session_id: str,
    base_branch: str,
    branch_name: str,
) -> None:
    """Self-review, then hand off to verify."""
    for attempt in range(MAX_REVIEW_RETRIES):
        review_prompt = build_review_prompt(base_branch)
        agent = create_agent(
            workspace, session_id=session_id, max_turns=20, task_id=task_id,
            home_dir=await home_dir_for_task(task),
            org_id=task.organization_id,
        )
        result = await agent.run(review_prompt, resume=True)
        review_output = result.output
        log.info(f"Review attempt {attempt + 1} for task #{task_id}: {review_output[:300]}...")

        if _is_cli_error(review_output):
            raise RuntimeError(
                f"agent CLI error during self-review: {review_output.strip()[:300]}"
            )

        if _review_passed(review_output):
            log.info(f"Self-review passed for task #{task_id}")
            break
        log.info(
            f"Self-review attempt {attempt + 1} did not emit REVIEW_PASSED for task #{task_id}; retrying"
        )
    else:
        log.warning(
            f"Self-review did not fully pass after {MAX_REVIEW_RETRIES} attempts for task #{task_id}"
        )

    # Hand off to verify; verify.pass_cycle calls _open_pr_and_advance.
    from agent.lifecycle import verify
    await transition_task(task_id, "verifying", "self-review complete; dispatching verify")
    await verify.handle_verify(task_id)


async def handle(event: Event) -> None:
    """EventBus entry — unpacks task.start_coding payload."""
    if not event.task_id:
        return
    retry_reason = event.payload.get("retry_reason") if event.payload else None
    await handle_coding(event.task_id, retry_reason=retry_reason)
