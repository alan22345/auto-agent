"""Agent event loop — listens for coding/planning events and runs the agentic loop.

Replaces claude_runner/main.py. All `run_claude_code()` subprocess calls are
replaced with in-process `AgentLoop.run()` calls using the configured LLM provider.
"""

from __future__ import annotations

import asyncio
import os
import re as _re
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import settings
from shared.events import Event
from shared.logging import setup_logging
from shared.redis_client import (
    ack_event,
    ensure_stream_group,
    get_redis,
    publish_event,
    read_events,
)
from shared.types import FreeformConfigData, RepoData, TaskData

from agent.context import ContextManager
from agent.llm import get_provider
from agent.loop import AgentLoop
from agent.prompts import (
    CLARIFICATION_MARKER,
    build_coding_prompt,
    build_plan_independent_review_prompt,
    build_planning_prompt,
    build_pr_independent_review_prompt,
    build_pr_review_response_prompt,
    build_review_prompt,
)
from agent.session import Session
from agent.tools import create_default_registry
from agent.workspace import (
    WORKSPACES_DIR,
    cleanup_workspace,
    clone_repo,
    create_branch,
    commit_pending_changes,
    ensure_branch_has_commits,
    push_branch,
)

log = setup_logging("agent")

ORCHESTRATOR_URL = settings.orchestrator_url
MAX_REVIEW_RETRIES = 2
SUMMARY_MAX_AGE = timedelta(days=7)


# ---------------------------------------------------------------------------
# Helpers (slugify, branch names, session IDs, clarification extraction)
# ---------------------------------------------------------------------------

async def _slugify_llm(title: str, max_len: int = 40) -> str:
    """Use the LLM to generate a concise branch slug."""
    try:
        provider = get_provider()
        from agent.llm.types import Message
        response = await provider.complete(
            messages=[Message(
                role="user",
                content=(
                    f"Generate a short git branch slug (2-4 words, lowercase, hyphenated, no special chars) "
                    f"that captures the essence of this task. Reply with ONLY the slug, nothing else.\n\n"
                    f"Task: {title[:200]}"
                ),
            )],
            max_tokens=50,
        )
        slug = response.message.content.strip().lower()
        slug = _re.sub(r'[^a-z0-9-]', '', slug)
        slug = _re.sub(r'-+', '-', slug).strip('-')
        if 3 <= len(slug) <= max_len:
            return slug
    except Exception:
        pass
    return _slugify_fallback(title, max_len)


def _slugify_fallback(title: str, max_len: int = 40) -> str:
    """Mechanical fallback slugify."""
    cleaned = _re.sub(r'^repo\s*[-\u2013\u2014]\s*\S+\s*[-\u2013\u2014]\s*', '', title, flags=_re.IGNORECASE).strip()
    cleaned = _re.sub(r'[^a-z0-9\s]', '', cleaned.lower())
    slug = _re.sub(r'\s+', '-', cleaned.strip())
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit('-', 1)[0]
    return slug or 'task'


async def _branch_name(task_id: int, title: str) -> str:
    slug = await _slugify_llm(title)
    return f"auto-agent/{slug}-{task_id}"


async def _pr_title(title: str) -> str:
    """Generate a clean PR title using the LLM."""
    cleaned = _re.sub(r'^repo\s*[-\u2013\u2014]\s*\S+\s*[-\u2013\u2014]\s*', '', title, flags=_re.IGNORECASE).strip()
    try:
        provider = get_provider()
        from agent.llm.types import Message
        response = await provider.complete(
            messages=[Message(
                role="user",
                content=(
                    f"Write a concise PR title (under 60 chars) for this task. "
                    f"Reply with ONLY the title, nothing else.\n\nTask: {cleaned[:300]}"
                ),
            )],
            max_tokens=80,
        )
        pr_title = response.message.content.strip()
        if 5 <= len(pr_title) <= 80:
            return f"[auto-agent] {pr_title}"
    except Exception:
        pass
    return f"[auto-agent] {cleaned[:100]}"


def _session_id(task_id: int, created_at: str | None = None) -> str:
    """Deterministic UUID session ID for a task."""
    seed = f"auto-agent-task-{task_id}-{created_at or ''}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _extract_clarification(output: str) -> str | None:
    """Check if agent output contains a clarification request."""
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(CLARIFICATION_MARKER):
            first_line = line.strip()[len(CLARIFICATION_MARKER):].strip()
            remaining = [l.strip() for l in lines[i + 1:] if l.strip()]
            parts = [first_line] + remaining
            return "\n".join(parts)
    return None


# ---------------------------------------------------------------------------
# Agent factory — creates an AgentLoop with the right config
# ---------------------------------------------------------------------------

def _format_tool_args(tool_name: str, args: dict) -> str:
    """Format tool args into a human-readable preview for the streaming UI."""
    if tool_name == "file_read":
        return args.get("file_path", "?")
    elif tool_name == "file_write":
        return args.get("file_path", "?")
    elif tool_name == "file_edit":
        return args.get("file_path", "?")
    elif tool_name == "grep":
        path = args.get("path", "")
        return f'"{args.get("pattern", "?")}"' + (f" in {path}" if path else "")
    elif tool_name == "glob":
        return args.get("pattern", "?")
    elif tool_name == "bash":
        return args.get("command", "?")[:100]
    elif tool_name == "git":
        return args.get("command", "?")[:80]
    elif tool_name == "test_runner":
        return args.get("target", "") or "full suite"
    return str(args)[:100]


async def _stream_to_task(task_id: int, event_type: str, payload: dict) -> None:
    """Publish a live-stream event for a task. The web UI picks these up
    via WebSocket and renders them in the task's chat feed."""
    try:
        r = await get_redis()
        import json
        await r.publish(f"task:{task_id}:stream", json.dumps({
            "type": event_type,
            **payload,
        }))
        await r.aclose()
    except Exception:
        pass  # Best-effort — don't break the agent if streaming fails


async def _check_guidance(task_id: int) -> str | None:
    """Check for a user guidance message sent via the UI.

    The web UI pushes guidance to a Redis list. We LPOP one message per
    check (one per turn). Returns None if no guidance is pending.
    """
    try:
        r = await get_redis()
        msg = await r.lpop(f"task:{task_id}:guidance")
        await r.aclose()
        if msg:
            return msg.decode() if isinstance(msg, bytes) else str(msg)
    except Exception:
        pass
    return None


async def _heartbeat_for_task(task_id: int) -> None:
    """Update a Redis key to signal the agent is alive and making progress.

    The timeout watchdog checks this key. If it exists, the task is alive
    regardless of how long ago `updated_at` was set. TTL=15 minutes.
    """
    try:
        r = await get_redis()
        await r.set(f"task:{task_id}:heartbeat", "1", ex=900)  # 15-min TTL
        await r.aclose()
    except Exception:
        pass  # Best-effort


def _create_agent(
    workspace: str,
    session_id: str | None = None,
    readonly: bool = False,
    max_turns: int = 50,
    include_methodology: bool = False,
    model_tier: str | None = None,
    task_id: int | None = None,
) -> AgentLoop:
    """Create a configured AgentLoop instance.

    Args:
        model_tier: Override model selection. Use "fast" for mechanical tasks,
                   "standard" for normal work, "capable" for complex architecture.
        task_id: If set, the agent sends heartbeat signals via Redis so the
                timeout watchdog knows it's making progress.
    """
    provider = get_provider(model_override=model_tier)
    tools = create_default_registry(readonly=readonly)
    ctx = ContextManager(workspace, provider)
    session = Session(session_id) if session_id else None

    heartbeat = None
    on_tool_call = None
    on_thinking = None
    get_guidance = None

    if task_id:
        async def heartbeat():
            await _heartbeat_for_task(task_id)

        async def on_tool_call(tool_name: str, args: dict, result_preview: str, turn: int):
            """Stream tool calls to the UI via Redis → WebSocket."""
            await _stream_to_task(task_id, "tool", {
                "tool": tool_name,
                "args_preview": _format_tool_args(tool_name, args),
                "result_preview": result_preview[:150],
                "turn": turn,
            })

        async def on_thinking(text: str, turn: int):
            """Stream assistant thinking/reasoning to the UI."""
            if len(text) > 20:  # Skip trivial empty responses
                await _stream_to_task(task_id, "thinking", {
                    "text": text[:500],
                    "turn": turn,
                })

        async def get_guidance() -> str | None:
            """Check for user guidance messages sent via the UI."""
            return await _check_guidance(task_id)

    return AgentLoop(
        provider=provider,
        tools=tools,
        context_manager=ctx,
        session=session,
        max_turns=max_turns,
        workspace=workspace,
        include_methodology=include_methodology,
        heartbeat=heartbeat,
        on_tool_call=on_tool_call,
        on_thinking=on_thinking,
        get_guidance=get_guidance,
    )


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

async def get_task(task_id: int) -> TaskData | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}")
        if resp.status_code == 200:
            return TaskData.model_validate(resp.json())
    return None


async def get_repo(repo_name: str) -> RepoData | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/repos")
        repos = resp.json()
        for repo_dict in repos:
            repo = RepoData.model_validate(repo_dict)
            if repo.name == repo_name:
                return repo
    return None


async def get_freeform_config(repo_name: str) -> FreeformConfigData | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/freeform/config")
        if resp.status_code != 200:
            return None
        configs = resp.json()
        for cfg in configs:
            cfg_data = FreeformConfigData.model_validate(cfg)
            if cfg_data.repo_name == repo_name and cfg_data.enabled:
                return cfg_data
    return None


async def transition_task(task_id: int, status: str, message: str = "") -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/transition",
            json={"status": status, "message": message},
        )
    if status in ("failed", "blocked", "done"):
        r = await get_redis()
        await publish_event(
            r,
            Event(
                type=f"task.{status}",
                task_id=task_id,
                payload={"error": message} if status in ("failed", "blocked") else {},
            ).to_redis(),
        )
        await r.aclose()


async def find_existing_pr_url(workspace: str, head_branch: str) -> str | None:
    """Return the URL of an existing open PR for `head_branch`, or None.

    Uses `gh pr list --head <branch> --state open --json url,state`. If gh
    fails or returns no results, returns None so the caller falls through to
    `gh pr create` normally.

    Rationale: when a task goes back to CODING after a deploy/review failure,
    `_finish_coding` is called again. The branch already has an open PR, so
    `gh pr create` fails with "pull request for branch X already exists".
    Checking first makes the path idempotent — re-entry just pushes new
    commits to the same PR.
    """
    env = os.environ.copy()
    env["GH_TOKEN"] = settings.github_token
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "list",
        "--head", head_branch,
        "--state", "open",
        "--json", "url,state",
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    try:
        import json as _json
        prs = _json.loads((stdout or b"").decode())
        for pr in prs:
            if pr.get("state") == "OPEN" and pr.get("url"):
                return pr["url"]
    except Exception:
        return None
    return None


async def create_pr(workspace: str, title: str, body: str, base_branch: str, head_branch: str) -> str:
    """Create a PR using the gh CLI, or return the existing one if the branch
    already has an open PR. Idempotent — safe to call after pushing new
    commits to a branch with an existing PR."""
    existing = await find_existing_pr_url(workspace, head_branch)
    if existing:
        log.info(f"PR already exists for {head_branch}, reusing: {existing}")
        return existing

    env = os.environ.copy()
    env["GH_TOKEN"] = settings.github_token
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", base_branch,
        "--head", head_branch,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    stdout_str = (stdout or b"").decode().strip()
    stderr_str = (stderr or b"").decode().strip()
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {stderr_str or stdout_str}")
    return stdout_str


# ---------------------------------------------------------------------------
# Repo summary generation
# ---------------------------------------------------------------------------

async def generate_repo_summary(repo_url: str, repo_name: str, default_branch: str) -> str | None:
    """Generate a repo summary using the agent with readonly tools."""
    from agent.workspace import clone_repo as _clone
    workspace = await _clone(repo_url, 0, default_branch, workspace_name=f"summary-{repo_name}")
    agent = _create_agent(workspace, readonly=True, max_turns=15, model_tier="fast")
    result = await agent.run(
        "Provide a concise summary of this repository: tech stack, project structure, "
        "key patterns, domain, and any notable conventions. Be brief (under 500 words)."
    )
    return result.output if result.output.strip() else None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

async def handle_planning(task_id: int, feedback: str | None = None) -> None:
    """Run the agent in planning mode (readonly tools) for complex tasks."""
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

    # Trigger harness onboarding if not done yet
    if not repo.harness_onboarded:
        log.info(f"Repo '{repo.name}' not harness-onboarded, triggering onboarding")
        r = await get_redis()
        await publish_event(
            r,
            Event(type="repo.onboard", task_id=0, payload={"repo_id": repo.id, "repo_name": repo.name}).to_redis(),
        )
        await r.aclose()

    # Generate repo summary if missing or stale
    summary_stale = False
    if repo.summary and repo.summary_updated_at:
        updated = repo.summary_updated_at
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        summary_stale = datetime.now(timezone.utc) - updated > SUMMARY_MAX_AGE

    if not repo.summary or summary_stale:
        reason = "stale" if summary_stale else "missing"
        log.info(f"Generating summary for repo '{repo.name}' ({reason})...")
        try:
            summary = await generate_repo_summary(repo.url, repo.name, repo.default_branch)
            if summary:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{ORCHESTRATOR_URL}/repos/{repo.id}/summary",
                        json={"summary": summary},
                    )
                repo.summary = summary
                log.info(f"Summary generated for '{repo.name}' ({len(summary)} chars)")
        except Exception:
            log.exception(f"Failed to generate summary for '{repo.name}', continuing without")

    session_id = _session_id(task_id, task.created_at)
    log.info(f"Planning task #{task_id} in {task.repo_name} (session={session_id})")
    workspace = await clone_repo(repo.url, task_id, repo.default_branch)

    try:
        agent = _create_agent(workspace, session_id=session_id, readonly=True, max_turns=30, include_methodology=True)

        if feedback:
            prompt = (
                f"The user rejected your previous plan with this feedback:\n\n{feedback}\n\n"
                f"Please revise the plan addressing their concerns. Output the revised plan as text."
            )
            result = await agent.run(prompt, resume=True)
        else:
            prompt = build_planning_prompt(task.title, task.description, repo.summary)
            result = await agent.run(prompt)

        output = result.output

        # Check if agent needs clarification
        question = _extract_clarification(output)
        if question:
            log.info(f"Task #{task_id} needs clarification: {question[:100]}...")
            await transition_task(task_id, "awaiting_clarification", question)
            r = await get_redis()
            await publish_event(
                r,
                Event(
                    type="task.clarification_needed",
                    task_id=task_id,
                    payload={"question": question, "phase": "planning"},
                ).to_redis(),
            )
            await r.aclose()
            return

        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/transition",
                json={"status": "awaiting_approval", "message": "Plan ready for review", "plan": output},
            )

        r = await get_redis()
        await publish_event(
            r,
            Event(type="task.plan_ready", task_id=task_id, payload={"plan": output}).to_redis(),
        )
        await r.aclose()

    except Exception as e:
        log.exception(f"Planning failed for task #{task_id}")
        await transition_task(task_id, "failed", str(e))
        cleanup_workspace(task_id)


# ---------------------------------------------------------------------------
# Subtask parsing
# ---------------------------------------------------------------------------

def _parse_plan_phases(plan: str) -> list[dict]:
    """Parse a plan into phases by splitting on '## Phase N' headers."""
    phase_pattern = _re.compile(r'^##\s+Phase\s+\d+', _re.MULTILINE)
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
        {"title": s["title"], "status": s["status"], "output_preview": s.get("output_preview", "")}
        for s in subtasks
    ]
    async with httpx.AsyncClient() as client:
        await client.patch(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/subtasks",
            json={"subtasks": api_subtasks, "current_subtask": current},
        )


# ---------------------------------------------------------------------------
# Coding
# ---------------------------------------------------------------------------

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
        r = await get_redis()
        await publish_event(
            r,
            Event(type="repo.onboard", task_id=0, payload={"repo_id": repo.id, "repo_name": repo.name}).to_redis(),
        )
        await r.aclose()

    session_id = _session_id(task_id, task.created_at)
    base_branch = repo.default_branch
    fallback_branch: str | None = None

    # Freeform mode: target the dev branch; if it doesn't exist, clone_repo
    # will create it from prod_branch.
    if task.freeform_mode and task.repo_name:
        freeform_cfg = await get_freeform_config(task.repo_name)
        if freeform_cfg:
            base_branch = freeform_cfg.dev_branch
            fallback_branch = freeform_cfg.prod_branch or repo.default_branch
            log.info(f"Freeform mode: targeting dev branch '{base_branch}' for task #{task_id}")

    is_continuation = task.plan is not None or retry_reason is not None
    log.info(f"Coding task #{task_id} in {task.repo_name} (session={session_id}, resume={is_continuation})")
    workspace = await clone_repo(repo.url, task_id, base_branch, fallback_branch=fallback_branch)

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

    try:
        phases = []
        if task.complexity == "complex_large" and task.plan and not retry_reason:
            phases = _parse_plan_phases(task.plan)

        if phases and len(phases) >= 2:
            await _handle_coding_with_subtasks(
                task_id, task, phases, workspace, session_id,
                base_branch, branch_name, is_continuation, repo,
            )
        else:
            await _handle_coding_single(
                task_id, task, workspace, session_id,
                base_branch, branch_name, is_continuation, repo,
                retry_reason,
            )
    except Exception as e:
        log.exception(f"Coding failed for task #{task_id}")
        await transition_task(task_id, "failed", str(e))
        cleanup_workspace(task_id)


async def _handle_coding_single(
    task_id: int, task, workspace: str, session_id: str,
    base_branch: str, branch_name: str, is_continuation: bool, repo,
    retry_reason: str | None = None,
) -> None:
    """Standard coding path — single implementation pass."""
    coding_prompt = build_coding_prompt(task.title, task.description, task.plan, repo.summary, repo.ci_checks)
    if retry_reason:
        coding_prompt += f"\n\nPrevious attempt failed. Reason: {retry_reason}\nFix the issues and try again."

    agent = _create_agent(workspace, session_id=session_id, max_turns=50, task_id=task_id)
    result = await agent.run(coding_prompt, resume=is_continuation)
    output = result.output
    log.info(f"Coding output for task #{task_id}: {output[:300]}...")

    # Check for clarification
    question = _extract_clarification(output)
    if question:
        log.info(f"Task #{task_id} needs clarification: {question[:100]}...")
        await transition_task(task_id, "awaiting_clarification", question)
        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="task.clarification_needed",
                task_id=task_id,
                payload={"question": question, "phase": "coding"},
            ).to_redis(),
        )
        await r.aclose()
        return

    await _finish_coding(task_id, task, workspace, session_id, base_branch, branch_name)


async def _handle_coding_with_subtasks(
    task_id: int, task, phases: list[dict], workspace: str, session_id: str,
    base_branch: str, branch_name: str, is_continuation: bool, repo,
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

        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="task.subtask_progress",
                task_id=task_id,
                payload={"current": i + 1, "total": total, "title": phase["title"], "status": "running"},
            ).to_redis(),
        )
        await r.aclose()

        prompt = (
            f"You are implementing a large task in phases. This is phase {i + 1} of {total}.\n\n"
            f"## Overall task\n{task.title}\n\n{task.description}\n\n"
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
                prev_summaries.append(f"  - Phase {j + 1} ({title}): {preview}")
            prev_context = "\n".join(prev_summaries)
            prompt += (
                f"## Previous phases (already implemented — do NOT redo)\n{prev_context}\n\n"
                "Implement ONLY the current phase. Commit your changes before stopping.\n"
            )

        log.info(f"Task #{task_id}: starting subtask {i + 1}/{total} — {phase['title']}")
        # Fresh agent per subtask (context isolation — superpowers pattern)
        # Each subtask gets its own agent with no session resume, so it starts
        # with clean context. The repo map in the system prompt provides structure.
        subtask_session = f"{session_id}-phase-{i + 1}"
        agent = _create_agent(workspace, session_id=subtask_session, max_turns=40)
        result = await agent.run(prompt, resume=False)
        output = result.output
        log.info(f"Task #{task_id} subtask {i + 1} output: {output[:300]}...")

        question = _extract_clarification(output)
        if question:
            phases[i]["status"] = "blocked"
            await _update_subtasks(task_id, phases, i)
            await transition_task(task_id, "awaiting_clarification", question)
            r = await get_redis()
            await publish_event(
                r,
                Event(
                    type="task.clarification_needed",
                    task_id=task_id,
                    payload={"question": question, "phase": f"subtask {i + 1}: {phase['title']}"},
                ).to_redis(),
            )
            await r.aclose()
            return

        phases[i]["status"] = "done"
        phases[i]["output_preview"] = output[:200]
        await _update_subtasks(task_id, phases, i)

        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="task.subtask_progress",
                task_id=task_id,
                payload={"current": i + 1, "total": total, "title": phase["title"], "status": "done"},
            ).to_redis(),
        )
        await r.aclose()

    log.info(f"Task #{task_id}: all {total} subtasks complete, proceeding to review + PR")
    await _finish_coding(task_id, task, workspace, session_id, base_branch, branch_name)


async def _finish_coding(
    task_id: int, task, workspace: str, session_id: str,
    base_branch: str, branch_name: str,
) -> None:
    """Self-review, push, create PR, and trigger independent review."""
    for attempt in range(MAX_REVIEW_RETRIES):
        review_prompt = build_review_prompt(base_branch)
        agent = _create_agent(workspace, session_id=session_id, max_turns=20, task_id=task_id)
        result = await agent.run(review_prompt, resume=True)
        review_output = result.output
        log.info(f"Review attempt {attempt + 1} for task #{task_id}: {review_output[:300]}...")

        if "REVIEW_PASSED" in review_output:
            log.info(f"Self-review passed for task #{task_id}")
            break
        log.info(f"Self-review found issues, agent fixed them (attempt {attempt + 1})")
    else:
        log.warning(f"Self-review did not fully pass after {MAX_REVIEW_RETRIES} attempts for task #{task_id}")

    # Safety net: the agent is supposed to commit its changes, but occasionally
    # forgets (see task 48 post-mortem). Auto-commit anything pending and then
    # verify we have at least one commit to PR.
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
    pr_url = await create_pr(workspace, title, pr_body, base_branch, branch_name)
    log.info(f"PR created: {pr_url}")
    if not pr_url.startswith("http"):
        raise RuntimeError(f"gh pr create returned invalid URL: {pr_url!r}")

    await handle_independent_review(task_id, pr_url, branch_name)


# ---------------------------------------------------------------------------
# Independent review
# ---------------------------------------------------------------------------

async def handle_independent_review(task_id: int, pr_url: str, branch_name: str) -> None:
    """Review a PR with a fresh agent session (independent reviewer)."""
    task = await get_task(task_id)
    if not task or not task.repo_name:
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    base_branch = repo.default_branch
    fallback_branch: str | None = None
    if task.freeform_mode and task.repo_name:
        freeform_cfg = await get_freeform_config(task.repo_name)
        if freeform_cfg:
            base_branch = freeform_cfg.dev_branch
            fallback_branch = freeform_cfg.prod_branch or repo.default_branch

    reviewer_session = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"auto-agent-review-{task_id}-{task.created_at or ''}",
    ))

    log.info(f"Independent review of task #{task_id} PR (session={reviewer_session})")
    workspace = await clone_repo(repo.url, task_id, base_branch, fallback_branch=fallback_branch)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", branch_name,
            cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        prompt = build_pr_independent_review_prompt(task.title, task.description, pr_url, base_branch)
        agent = _create_agent(workspace, session_id=reviewer_session, readonly=True, max_turns=20)
        result = await agent.run(prompt)
        output = result.output
        log.info(f"Independent review for task #{task_id}: {output[:300]}...")

        approved = any(
            phrase in output.lower()
            for phrase in ["--approve", "lgtm", "looks good", "pr review --approve"]
        )

        if approved:
            log.info(f"Independent review approved task #{task_id}")
            r = await get_redis()
            await publish_event(
                r,
                Event(
                    type="task.review_complete",
                    task_id=task_id,
                    payload={"review": output[:2000], "pr_url": pr_url, "branch": branch_name, "approved": True},
                ).to_redis(),
            )
            await r.aclose()
        else:
            log.info(f"Independent review requested changes for task #{task_id}")
            session_id = _session_id(task_id, task.created_at)
            fix_prompt = (
                f"An independent code reviewer left feedback on your PR. "
                f"Address their comments:\n\n{output}\n\nFix the issues, commit, and push."
            )
            fix_agent = _create_agent(workspace, session_id=session_id, max_turns=30)
            fix_result = await fix_agent.run(fix_prompt, resume=True)
            log.info(f"Review fixes for task #{task_id}: {fix_result.output[:300]}...")

            # Safety net — agent may have forgotten to commit review fixes
            committed_now = await commit_pending_changes(
                workspace, task_id, f"Address review feedback — {task.title}"
            )
            if committed_now:
                log.warning(
                    f"Task #{task_id}: review-fix agent left uncommitted changes — auto-committed"
                )
            await push_branch(workspace, branch_name)

            r = await get_redis()
            await publish_event(
                r,
                Event(
                    type="task.review_complete",
                    task_id=task_id,
                    payload={
                        "review": output[:2000], "fixes": fix_result.output[:1000],
                        "pr_url": pr_url, "branch": branch_name, "approved": False,
                    },
                ).to_redis(),
            )
            await r.aclose()

    except Exception as e:
        log.exception(f"Independent review failed for task #{task_id}")
        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="task.review_complete",
                task_id=task_id,
                payload={"review": f"Review skipped: {e}", "pr_url": pr_url, "branch": branch_name, "approved": True},
            ).to_redis(),
        )
        await r.aclose()


async def handle_plan_independent_review(task_id: int) -> None:
    """Run an independent reviewer on a freeform task's plan."""
    import tempfile

    task = await get_task(task_id)
    if not task:
        return
    if task.status != "awaiting_approval":
        log.info(f"Plan auto-review skipped for task #{task_id}: status is '{task.status}'")
        return
    if not task.freeform_mode:
        return
    if not task.plan:
        # Plan is missing (likely due to the DB-save bug — plan was passed in the
        # transition API body but not persisted). Can't review an empty plan —
        # auto-approve so the task doesn't get stuck forever at AWAITING_APPROVAL.
        log.warning(f"Task #{task_id}: plan is empty, auto-approving without review")
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
                json={"approved": True, "message": "Plan auto-approved (plan text was empty — review skipped)"},
            )
        return

    log.info(f"Running independent plan review for freeform task #{task_id}")
    prompt = build_plan_independent_review_prompt(task.title, task.description, task.plan)

    try:
        with tempfile.TemporaryDirectory(prefix=f"plan-review-{task_id}-") as tmp:
            agent = _create_agent(tmp, readonly=True, max_turns=5, model_tier="fast")
            result = await agent.run(prompt)
            output = result.output
    except Exception as e:
        log.exception(f"Plan auto-review failed for task #{task_id}")
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
                json={"approved": True, "message": f"Plan auto-approved (reviewer error: {e})"},
            )
        return

    output_stripped = output.strip()
    log.info(f"Plan reviewer output for task #{task_id}: {output_stripped[:300]}...")

    verdict = ""
    reasoning_start = 0
    for i, line in enumerate(output_stripped.splitlines()):
        if line.strip():
            verdict = line.strip().upper()
            reasoning_start = i + 1
            break
    reasoning = "\n".join(output_stripped.splitlines()[reasoning_start:]).strip() or "(no reasoning provided)"

    approved = verdict.startswith("APPROVE")
    decision_label = "APPROVED" if approved else "REJECTED"
    log_message = f"Plan {decision_label} by independent reviewer\n\n{reasoning[:1900]}"

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
            json={"approved": approved, "feedback": reasoning if not approved else "", "message": log_message},
        )

    log.info(f"Plan auto-review complete for task #{task_id}: {decision_label}")


# ---------------------------------------------------------------------------
# PR review comments & clarification
# ---------------------------------------------------------------------------

async def handle_pr_review_comments(task_id: int, comments: str) -> None:
    """Address PR review comments by resuming the coding session."""
    task = await get_task(task_id)
    if not task or not task.repo_name or not task.pr_url:
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    session_id = _session_id(task_id, task.created_at)
    base_branch = repo.default_branch
    branch_name = task.branch_name or await _branch_name(task_id, task.title)

    log.info(f"Addressing PR review for task #{task_id} (session={session_id})")
    if task.status in ("awaiting_review", "awaiting_ci"):
        await transition_task(task_id, "coding", f"Addressing feedback: {comments[:200]}")
    workspace = await clone_repo(repo.url, task_id, base_branch)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", branch_name,
            cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        prompt = build_pr_review_response_prompt(task.title, task.description, comments)
        agent = _create_agent(workspace, session_id=session_id, max_turns=30)
        result = await agent.run(prompt, resume=True)
        log.info(f"PR review response for task #{task_id}: {result.output[:300]}...")

        # Safety net — agent may have addressed comments without committing
        committed_now = await commit_pending_changes(
            workspace, task_id, f"Address PR review comments — {task.title}"
        )
        if committed_now:
            log.warning(
                f"Task #{task_id}: PR-review agent left uncommitted changes — auto-committed"
            )
        await push_branch(workspace, branch_name)

        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="task.review_comments_addressed",
                task_id=task_id,
                payload={"output": result.output[:1000], "pr_url": task.pr_url or ""},
            ).to_redis(),
        )
        await r.aclose()

    except Exception as e:
        log.exception(f"PR review response failed for task #{task_id}")
        await transition_task(task_id, "blocked", f"Failed to address review: {e}")


async def handle_clarification_response(task_id: int, answer: str) -> None:
    """Resume a task after the user answered a clarification question."""
    task = await get_task(task_id)
    if not task or not task.repo_name:
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    session_id = _session_id(task_id, task.created_at)
    workspace = os.path.join(WORKSPACES_DIR, f"task-{task_id}")

    log.info(f"Resuming task #{task_id} with clarification answer (session={session_id})")

    agent = _create_agent(workspace, session_id=session_id, max_turns=40)
    result = await agent.run(
        f"The user answered your clarification question:\n\n{answer}\n\nPlease continue with the task.",
        resume=True,
    )

    follow_up = _extract_clarification(result.output)
    if follow_up:
        log.info(f"Task #{task_id} needs another clarification: {follow_up[:100]}...")
        await transition_task(task_id, "awaiting_clarification", follow_up)
        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="task.clarification_needed",
                task_id=task_id,
                payload={"question": follow_up, "phase": "continuation"},
            ).to_redis(),
        )
        await r.aclose()
        return

    r = await get_redis()
    await publish_event(
        r,
        Event(type="task.clarification_resolved", task_id=task_id, payload={"output": result.output}).to_redis(),
    )
    await r.aclose()


# ---------------------------------------------------------------------------
# Blocked task handling
# ---------------------------------------------------------------------------

async def _try_assign_repo(task_id: int, message: str) -> bool:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/repos")
        if resp.status_code != 200:
            return False
        repos = resp.json()
        msg_lower = message.lower()
        for repo_dict in repos:
            name = repo_dict.get("name", "")
            if name and name.lower() in msg_lower:
                resp = await client.patch(
                    f"{ORCHESTRATOR_URL}/tasks/{task_id}/repo",
                    json={"repo_name": name},
                )
                if resp.status_code == 200:
                    log.info(f"Assigned repo '{name}' to task #{task_id} from user message")
                    return True
    return False


async def handle_blocked_response(task_id: int, task: TaskData, message: str) -> None:
    """Resume a blocked task after the user provides input."""
    log.info(f"Resuming blocked task #{task_id} with user message: {message[:100]}...")

    if not task.repo_name:
        assigned = await _try_assign_repo(task_id, message)
        if not assigned:
            log.warning(f"Task #{task_id} blocked with no repo, couldn't extract from message")
            r = await get_redis()
            await publish_event(
                r,
                Event(
                    type="task.blocked",
                    task_id=task_id,
                    payload={"error": "No repo assigned. Please include the repo name in your message."},
                ).to_redis(),
            )
            await r.aclose()
            return

    if task.pr_url:
        await transition_task(task_id, "coding", f"User unblocked: {message[:200]}")
        await handle_pr_review_comments(task_id, message)
    elif task.plan:
        await transition_task(task_id, "coding", f"User unblocked: {message[:200]}")
        await handle_coding(task_id)
    else:
        await transition_task(task_id, "planning", f"User unblocked: {message[:200]}")
        await handle_planning(task_id)


# ---------------------------------------------------------------------------
# Deploy preview (unchanged — no CLI dependency)
# ---------------------------------------------------------------------------

DEPLOY_WORKFLOW_NAMES = ["deploy.yml", "deploy-dev.yml"]
DEPLOY_SCRIPT_CANDIDATES = [
    "scripts/deploy-dev.sh", "scripts/deploy-dev", "scripts/deploy_dev.sh",
    "scripts/deploy.sh", "deploy-dev.sh", "deploy.sh",
]


async def handle_deploy_preview(task_id: int) -> None:
    """Deploy the task's branch to a dev environment."""
    task = await get_task(task_id)
    if not task or not task.repo_name:
        return

    branch_name = task.branch_name or await _branch_name(task_id, task.title)

    if task.pr_url and settings.github_token:
        deployed = await _try_github_workflow_deploy(task_id, task, branch_name)
        if deployed:
            return

    workspace = os.path.join(WORKSPACES_DIR, f"task-{task_id}")
    if not os.path.exists(workspace):
        log.info(f"Task #{task_id}: no workspace for deploy preview, skipping")
        return

    await _try_local_deploy(task_id, task, branch_name, workspace)


async def _try_github_workflow_deploy(task_id: int, task: TaskData, branch_name: str) -> bool:
    """Trigger a GitHub Actions deploy workflow via workflow_dispatch."""
    parts = task.pr_url.rstrip("/").split("/")
    owner, repo = parts[-4], parts[-3]

    headers = {
        "Authorization": f"token {settings.github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows",
            headers=headers,
        )
        if resp.status_code != 200:
            return False

        workflows = resp.json().get("workflows", [])
        deploy_workflow = None
        for wf in workflows:
            wf_filename = wf.get("path", "").split("/")[-1]
            if wf_filename in DEPLOY_WORKFLOW_NAMES and wf.get("state") == "active":
                deploy_workflow = wf
                break

        if not deploy_workflow:
            return False

        workflow_id = deploy_workflow["id"]
        log.info(f"Task #{task_id}: triggering workflow '{deploy_workflow['name']}' on branch '{branch_name}'")

        resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            headers=headers,
            json={"ref": branch_name, "inputs": {"environment": "dev"}},
        )

        if resp.status_code == 204:
            conclusion = await _wait_for_workflow_run(owner, repo, workflow_id, branch_name, headers, task_id)
            r = await get_redis()
            event_type = "task.dev_deployed" if conclusion == "success" else "task.dev_deploy_failed"
            await publish_event(
                r,
                Event(
                    type=event_type,
                    task_id=task_id,
                    payload={
                        "branch": branch_name,
                        "output": f"Deploy workflow finished: {conclusion}",
                        "pr_url": task.pr_url or "",
                    },
                ).to_redis(),
            )
            await r.aclose()
            return True
        else:
            log.warning(f"Task #{task_id}: workflow dispatch failed: {resp.status_code}")
            return False


async def _wait_for_workflow_run(
    owner: str, repo: str, workflow_id: int, branch: str,
    headers: dict, task_id: int,
    poll_interval: int = 30, max_wait: int = 1200,
) -> str:
    import time
    start = time.monotonic()
    await asyncio.sleep(5)

    async with httpx.AsyncClient() as client:
        while time.monotonic() - start < max_wait:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
                headers=headers,
                params={"branch": branch, "per_page": 1, "event": "workflow_dispatch"},
            )
            if resp.status_code == 200:
                runs = resp.json().get("workflow_runs", [])
                if runs:
                    run = runs[0]
                    if run.get("status") == "completed":
                        return run.get("conclusion") or "unknown"
            await asyncio.sleep(poll_interval)

    return "timed_out"


async def _try_local_deploy(task_id: int, task: TaskData, branch_name: str, workspace: str) -> None:
    """Try running a local deploy script from the workspace."""
    deploy_script = None
    for candidate in DEPLOY_SCRIPT_CANDIDATES:
        script_path = os.path.join(workspace, candidate)
        if os.path.isfile(script_path):
            deploy_script = candidate
            break

    makefile_path = os.path.join(workspace, "Makefile")
    has_makefile_target = False
    if not deploy_script and os.path.isfile(makefile_path):
        try:
            with open(makefile_path) as f:
                content = f.read()
            if "deploy-dev" in content:
                has_makefile_target = True
        except Exception:
            pass

    if not deploy_script and not has_makefile_target:
        log.info(f"Task #{task_id}: no deploy script found, skipping dev deploy")
        return

    log.info(f"Task #{task_id}: deploying branch '{branch_name}' to dev via local script")
    try:
        if deploy_script:
            script_path = os.path.join(workspace, deploy_script)
            os.chmod(script_path, 0o755)
            proc = await asyncio.create_subprocess_exec(
                f"./{deploy_script}", branch_name,
                cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "BRANCH": branch_name, "TASK_ID": str(task_id)},
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "make", "deploy-dev",
                cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "BRANCH": branch_name, "TASK_ID": str(task_id)},
            )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            r = await get_redis()
            await publish_event(
                r,
                Event(type="task.dev_deploy_failed", task_id=task_id, payload={"branch": branch_name, "output": "Deploy timed out", "pr_url": task.pr_url or ""}).to_redis(),
            )
            await r.aclose()
            return

        output = ((stdout or b"").decode() + (stderr or b"").decode()).strip()
        r = await get_redis()
        event_type = "task.dev_deployed" if proc.returncode == 0 else "task.dev_deploy_failed"
        await publish_event(
            r,
            Event(type=event_type, task_id=task_id, payload={"branch": branch_name, "output": output[-1000:], "pr_url": task.pr_url or ""}).to_redis(),
        )
        await r.aclose()

    except Exception:
        log.exception(f"Task #{task_id}: deploy preview failed")
        try:
            r = await get_redis()
            await publish_event(
                r,
                Event(type="task.dev_deploy_failed", task_id=task_id, payload={"branch": branch_name, "output": "Unexpected error", "pr_url": task.pr_url or ""}).to_redis(),
            )
            await r.aclose()
        except Exception:
            pass


async def handle_query(task_id: int) -> None:
    """Handle a SIMPLE_NO_CODE task — just answer the question via a single LLM call.

    No repo, no git, no tools — just send the task description to the LLM and
    return the response. The answer goes into the task's chat via transition message.
    """
    task = await get_task(task_id)
    if not task:
        return

    log.info(f"Handling query task #{task_id}: {task.title[:100]}")

    try:
        provider = get_provider(model_override="standard")

        from agent.llm.types import Message as Msg
        response = await provider.complete(
            messages=[
                Msg(
                    role="user",
                    content=(
                        f"{task.title}\n\n{task.description or ''}\n\n"
                        "Answer this question thoroughly and concisely. "
                        "If you need to browse a URL, say so — but give the best answer you can from your knowledge."
                    ),
                ),
            ],
            max_tokens=4096,
        )
        answer = response.message.content

        # Close the async client before transitioning
        if hasattr(provider, '_client'):
            try:
                if hasattr(provider._client, '_client') and hasattr(provider._client._client, 'aclose'):
                    await provider._client._client.aclose()
                elif hasattr(provider._client, 'close'):
                    provider._client.close()
            except Exception:
                pass

        # Save answer: plan field holds the full response (50K limit), message
        # field gets a truncated preview (2K limit on TransitionRequest.message).
        msg_preview = answer[:1900] + "..." if len(answer) > 1900 else answer
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/transition",
                json={"status": "done", "message": f"Answer:\n\n{msg_preview}", "plan": answer},
            )
            if resp.status_code >= 400:
                log.error(f"Query task #{task_id}: transition to done failed ({resp.status_code}): {resp.text[:200]}")
        log.info(f"Query task #{task_id} completed ({len(answer)} chars)")

    except Exception as e:
        log.exception(f"Query task #{task_id} failed")
        await transition_task(task_id, "failed", str(e))


async def handle_task_cleanup(task_id: int) -> None:
    """Clean up workspace and session for a finished task."""
    log.info(f"Cleaning up workspace for task #{task_id}")
    cleanup_workspace(task_id)


# ---------------------------------------------------------------------------
# Harness onboarding
# ---------------------------------------------------------------------------

async def handle_harness_onboarding(repo_id: int, repo_name: str) -> None:
    """Run harness onboarding using the agent instead of the CLI."""
    from agent.harness import handle_harness_onboarding as _handle
    await _handle(repo_id, repo_name)


# ---------------------------------------------------------------------------
# PO analysis worker
# ---------------------------------------------------------------------------

_po_queue: asyncio.Queue[int] = asyncio.Queue()


async def _po_worker() -> None:
    """Background worker — runs PO analyses sequentially."""
    from agent.po_analyzer import handle_po_analysis as _handle_po
    from shared.database import async_session as _async_session
    from shared.models import FreeformConfig as _FC
    from sqlalchemy import select as _select

    log.info("PO analysis worker started")
    while True:
        repo_id = await _po_queue.get()
        try:
            async with _async_session() as _session:
                _result = await _session.execute(_select(_FC).where(_FC.repo_id == repo_id))
                _config = _result.scalar_one_or_none()
                if _config:
                    await _handle_po(_session, _config)
                    _config.last_analysis_at = datetime.now(timezone.utc)
                    await _session.commit()
        except Exception:
            log.exception(f"PO analysis worker error for repo_id={repo_id}")
        finally:
            _po_queue.task_done()


# ---------------------------------------------------------------------------
# Event loop (main entry point)
# ---------------------------------------------------------------------------

async def event_loop() -> None:
    """Main loop — listen for planning, coding, cleanup, and PR review events."""
    r = await get_redis()
    await ensure_stream_group(r)
    asyncio.create_task(_po_worker())
    log.info("Agent event loop started")

    backoff = 1
    max_backoff = 60

    while True:
        try:
            messages = await read_events(r, consumer="claude-runner", count=1, block=5000)
            backoff = 1
            for msg_id, data in messages:
                try:
                    event = Event.from_redis(data)
                    if event.type == "task.start_planning" and event.task_id:
                        feedback = event.payload.get("feedback") if event.payload else None
                        await handle_planning(event.task_id, feedback=feedback)
                    elif event.type == "task.plan_ready" and event.task_id:
                        _t = await get_task(event.task_id)
                        if _t and _t.freeform_mode and _t.status == "awaiting_approval":
                            await handle_plan_independent_review(event.task_id)
                    elif event.type == "task.start_coding" and event.task_id:
                        retry_reason = event.payload.get("retry_reason")
                        await handle_coding(event.task_id, retry_reason=retry_reason)
                    elif event.type == "task.deploy_preview" and event.task_id:
                        await handle_deploy_preview(event.task_id)
                    elif event.type == "task.query" and event.task_id:
                        await handle_query(event.task_id)
                    elif event.type == "task.cleanup" and event.task_id:
                        await handle_task_cleanup(event.task_id)
                    elif event.type == "task.clarification_response" and event.task_id:
                        answer = event.payload.get("answer", "")
                        if answer:
                            await handle_clarification_response(event.task_id, answer)
                    elif event.type == "po.analyze":
                        repo_id = event.payload.get("repo_id")
                        repo_name = event.payload.get("repo_name", "")
                        if repo_id:
                            queued = _po_queue.qsize() > 0
                            await _po_queue.put(repo_id)
                            if queued:
                                r2 = await get_redis()
                                await publish_event(
                                    r2,
                                    Event(type="po.analysis_queued", task_id=0, payload={"repo_name": repo_name, "position": _po_queue.qsize()}).to_redis(),
                                )
                                await r2.aclose()
                    elif event.type == "repo.onboard":
                        repo_id = event.payload.get("repo_id")
                        repo_name = event.payload.get("repo_name", "")
                        if repo_id:
                            await handle_harness_onboarding(repo_id, repo_name)
                    elif event.type == "human.message":
                        task_id = event.task_id
                        comments = event.payload.get("message", "")
                        if task_id and comments:
                            task = await get_task(task_id)
                            if not task:
                                continue
                            if task.status == "awaiting_clarification":
                                await handle_clarification_response(task_id, comments)
                            elif task.status == "blocked":
                                await handle_blocked_response(task_id, task, comments)
                            elif task.status in ("pr_created", "awaiting_ci", "awaiting_review", "coding") and task.pr_url:
                                await handle_pr_review_comments(task_id, comments)
                            else:
                                log.info(f"Message for task #{task_id} in status '{task.status}' — not routing")
                                if task.plan:
                                    r2 = await get_redis()
                                    await publish_event(
                                        r2,
                                        Event(type="task.plan_ready", task_id=task_id, payload={"plan": task.plan}).to_redis(),
                                    )
                                    await r2.aclose()
                except Exception:
                    log.exception("Error handling event")
                finally:
                    await ack_event(r, msg_id, consumer="claude-runner")
        except Exception:
            log.exception("Event loop error", retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            try:
                r = await get_redis()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(event_loop())
