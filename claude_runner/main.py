"""Claude Code runner — listens for coding/planning events and executes Claude Code CLI."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta

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
from shared.types import RepoData, TaskData

from claude_runner.harness import handle_harness_onboarding
from claude_runner.prompts import (
    CLARIFICATION_MARKER,
    build_coding_prompt,
    build_planning_prompt,
    build_pr_independent_review_prompt,
    build_pr_review_response_prompt,
    build_review_prompt,
)
from claude_runner.summarizer import generate_repo_summary
from claude_runner.workspace import (
    WORKSPACES_DIR,
    cleanup_workspace,
    clone_repo,
    create_branch,
    push_branch,
)

log = setup_logging("claude-runner")

ORCHESTRATOR_URL = settings.orchestrator_url
MAX_REVIEW_RETRIES = 2
SUMMARY_MAX_AGE = timedelta(days=7)


def _session_id(task_id: int, created_at: str | None = None) -> str:
    """Deterministic UUID session ID for a task — all Claude invocations for the
    same task share this session so context is preserved across phases.

    Includes created_at to avoid collisions when task IDs are recycled."""
    seed = f"auto-agent-task-{task_id}-{created_at or ''}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _extract_clarification(output: str) -> str | None:
    """Check if Claude's output contains a clarification request.

    Captures the marker line and all subsequent lines as the question,
    since clarification questions may span multiple lines.

    Returns the question text if found, None otherwise.
    """
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(CLARIFICATION_MARKER):
            first_line = line.strip()[len(CLARIFICATION_MARKER):].strip()
            # Capture remaining lines as continuation of the question
            remaining = [l.strip() for l in lines[i + 1:] if l.strip()]
            parts = [first_line] + remaining
            return "\n".join(parts)
    return None


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


async def transition_task(task_id: int, status: str, message: str = "") -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/transition",
            json={"status": status, "message": message},
        )
    # Publish an event for terminal transitions so notifications fire
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


async def run_claude_code(
    workspace: str,
    prompt: str,
    timeout: int = 600,
    session_id: str | None = None,
    resume: bool = False,
) -> str:
    """Run Claude Code CLI in the workspace with the given prompt. Returns output.

    Uses your local Claude Max auth via OS keychain.
    Runs as an async subprocess to avoid blocking the event loop.

    Args:
        session_id: If provided, ties this invocation to a persistent session
                    so Claude retains context across calls for the same task.
        resume: If True, continues an existing session (requires session_id).
    """
    cmd = ["claude", "--print", "--dangerously-skip-permissions"]
    if session_id:
        if resume:
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", session_id]
    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"Claude Code timed out after {timeout}s")
    return (stdout or b"").decode() + (stderr or b"").decode()


async def create_pr(workspace: str, title: str, body: str, base_branch: str, head_branch: str) -> str:
    """Create a PR using the gh CLI. Returns PR URL."""
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


async def handle_planning(task_id: int, feedback: str | None = None) -> None:
    """Run Claude Code in planning mode for complex tasks.

    If feedback is provided (from a rejected plan), Claude resumes the session
    and revises the plan based on the feedback.
    """
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

    # Trigger harness onboarding if not done yet (non-blocking — runs in background)
    if not repo.harness_onboarded:
        log.info(f"Repo '{repo.name}' not harness-onboarded, triggering onboarding")
        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="repo.onboard",
                task_id=0,
                payload={"repo_id": repo.id, "repo_name": repo.name},
            ).to_redis(),
        )
        await r.aclose()

    # Generate repo summary if missing or stale — saves context on future tasks
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
        if feedback:
            # Resume session and revise plan based on feedback
            prompt = (
                f"The user rejected your previous plan with this feedback:\n\n{feedback}\n\n"
                f"Please revise the plan addressing their concerns. Output the revised plan as text."
            )
            output = await run_claude_code(workspace, prompt, timeout=1200, session_id=session_id, resume=True)
        else:
            prompt = build_planning_prompt(task.title, task.description, repo.summary)
            output = await run_claude_code(workspace, prompt, timeout=1200, session_id=session_id)

        # Fallback: if stdout is empty, Claude may have written the plan to a file
        if not output.strip():
            plan_dir = os.path.expanduser("~/.claude/plans")
            if os.path.isdir(plan_dir):
                plan_files = sorted(
                    (os.path.join(plan_dir, f) for f in os.listdir(plan_dir)),
                    key=os.path.getmtime,
                    reverse=True,
                )
                if plan_files:
                    with open(plan_files[0]) as f:
                        output = f.read()
                    log.info(f"Read plan from file: {plan_files[0]}")

        # Check if Claude needs clarification before proceeding
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
                json={
                    "status": "awaiting_approval",
                    "message": f"Plan:\n{output}",
                },
            )

        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="task.plan_ready",
                task_id=task_id,
                payload={"plan": output},
            ).to_redis(),
        )
        await r.aclose()

    except Exception as e:
        log.exception(f"Planning failed for task #{task_id}")
        await transition_task(task_id, "failed", str(e))
        cleanup_workspace(task_id)


async def handle_coding(task_id: int, retry_reason: str | None = None) -> None:
    """Run Claude Code to implement, self-review, test, and create a PR.

    Uses a persistent session so Claude retains context from planning and
    previous coding attempts (e.g. CI failure retries).
    """
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

    # Trigger harness onboarding if not done yet (non-blocking — runs in background)
    if not repo.harness_onboarded:
        log.info(f"Repo '{repo.name}' not harness-onboarded, triggering onboarding")
        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="repo.onboard",
                task_id=0,
                payload={"repo_id": repo.id, "repo_name": repo.name},
            ).to_redis(),
        )
        await r.aclose()

    session_id = _session_id(task_id, task.created_at)
    base_branch = repo.default_branch
    # Resume session if this task already had a planning or previous coding run
    is_continuation = task.plan is not None or retry_reason is not None
    log.info(
        f"Coding task #{task_id} in {task.repo_name} "
        f"(session={session_id}, resume={is_continuation})"
    )
    workspace = await clone_repo(repo.url, task_id, base_branch)
    branch_name = f"auto-agent/task-{task_id}"

    # Create or checkout the task branch (idempotent)
    await create_branch(workspace, branch_name)

    try:
        # Step 1: Implement the task
        coding_prompt = build_coding_prompt(task.title, task.description, task.plan, repo.summary, repo.ci_checks)
        if retry_reason:
            coding_prompt += f"\n\nPrevious attempt failed. Reason: {retry_reason}\nFix the issues and try again."
        output = await run_claude_code(
            workspace, coding_prompt, timeout=1800,
            session_id=session_id, resume=is_continuation,
        )
        log.info(f"Coding output for task #{task_id}: {output[:300]}...")

        # Check if Claude needs clarification before proceeding
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

        # Step 2: Self-review loop (always resumes within the same session)
        for attempt in range(MAX_REVIEW_RETRIES):
            review_prompt = build_review_prompt(base_branch)
            review_output = await run_claude_code(
                workspace, review_prompt, timeout=300,
                session_id=session_id, resume=True,
            )
            log.info(
                f"Review attempt {attempt + 1} for task #{task_id}: {review_output[:300]}..."
            )

            if "REVIEW_PASSED" in review_output:
                log.info(f"Self-review passed for task #{task_id}")
                break
            log.info(
                f"Self-review found issues, Claude fixed them (attempt {attempt + 1})"
            )
        else:
            log.warning(
                f"Self-review did not fully pass after {MAX_REVIEW_RETRIES} attempts for task #{task_id}"
            )

        # Step 3: Push and create PR
        await push_branch(workspace, branch_name)
        pr_body = (
            f"## Auto-Agent Task #{task_id}\n\n"
            f"**Task:** {task.title}\n\n"
            f"**Description:** {task.description[:500]}\n\n"
            f"---\n"
            f"*Generated by auto-agent via Claude Code. "
            f"Code was self-reviewed for correctness, security, and root-cause analysis.*"
        )
        pr_url = await create_pr(workspace, task.title, pr_body, base_branch, branch_name)
        log.info(f"PR created: {pr_url}")
        if not pr_url.startswith("http"):
            raise RuntimeError(f"gh pr create returned invalid URL: {pr_url!r}")

        # Step 4: Trigger independent review (separate session)
        await handle_independent_review(task_id, pr_url, branch_name)

    except Exception as e:
        log.exception(f"Coding failed for task #{task_id}")
        await transition_task(task_id, "failed", str(e))
        cleanup_workspace(task_id)


async def handle_independent_review(task_id: int, pr_url: str, branch_name: str) -> None:
    """Review a PR with a fresh Claude session (independent reviewer).

    Uses a completely separate session from the one that wrote the code,
    giving an unbiased review.
    """
    task = await get_task(task_id)
    if not task or not task.repo_name:
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    base_branch = repo.default_branch
    # Use a different session ID for the reviewer (append "-review")
    reviewer_session = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"auto-agent-review-{task_id}-{task.created_at or ''}",
    ))

    log.info(f"Independent review of task #{task_id} PR (session={reviewer_session})")
    workspace = await clone_repo(repo.url, task_id, base_branch)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", branch_name,
            cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        prompt = build_pr_independent_review_prompt(
            task.title, task.description, pr_url, base_branch,
        )
        output = await run_claude_code(
            workspace, prompt, timeout=600,
            session_id=reviewer_session, resume=False,
        )
        log.info(f"Independent review for task #{task_id}: {output[:300]}...")

        # Check if reviewer approved or requested changes
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
                    payload={
                        "review": output[:2000],
                        "pr_url": pr_url,
                        "branch": branch_name,
                        "approved": True,
                    },
                ).to_redis(),
            )
            await r.aclose()
        else:
            log.info(f"Independent review requested changes for task #{task_id}")
            # Resume the coding session (session A) to address review feedback
            session_id = _session_id(task_id, task.created_at)
            fix_prompt = (
                f"An independent code reviewer left feedback on your PR. "
                f"Address their comments:\n\n{output}\n\n"
                f"Fix the issues, commit, and push."
            )
            fix_output = await run_claude_code(
                workspace, fix_prompt, timeout=900,
                session_id=session_id, resume=True,
            )
            log.info(f"Review fixes for task #{task_id}: {fix_output[:300]}...")

            # Push the fixes
            await push_branch(workspace, branch_name)

            r = await get_redis()
            await publish_event(
                r,
                Event(
                    type="task.review_complete",
                    task_id=task_id,
                    payload={
                        "review": output[:2000],
                        "fixes": fix_output[:1000],
                        "pr_url": pr_url,
                        "branch": branch_name,
                        "approved": False,
                    },
                ).to_redis(),
            )
            await r.aclose()

    except Exception as e:
        log.exception(f"Independent review failed for task #{task_id}")
        # Don't fail the task — just skip review and proceed
        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="task.review_complete",
                task_id=task_id,
                payload={
                    "review": f"Review skipped: {e}",
                    "pr_url": pr_url,
                    "branch": branch_name,
                    "approved": True,
                },
            ).to_redis(),
        )
        await r.aclose()


async def handle_pr_review_comments(task_id: int, comments: str) -> None:
    """Address PR review comments by re-running Claude Code with the feedback.

    Resumes the existing session so Claude has full context of what it built.
    """
    task = await get_task(task_id)
    if not task:
        return

    if not task.repo_name:
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    session_id = _session_id(task_id, task.created_at)
    base_branch = repo.default_branch
    branch_name = f"auto-agent/task-{task_id}"

    log.info(f"Addressing PR review for task #{task_id} (session={session_id})")
    workspace = await clone_repo(repo.url, task_id, base_branch)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", branch_name,
            cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        prompt = build_pr_review_response_prompt(task.title, task.description, comments)
        output = await run_claude_code(
            workspace, prompt, timeout=600,
            session_id=session_id, resume=True,
        )
        log.info(f"PR review response for task #{task_id}: {output[:300]}...")

        await push_branch(workspace, branch_name)

        # Notify that review comments have been addressed
        r = await get_redis()
        await publish_event(
            r,
            Event(
                type="task.review_comments_addressed",
                task_id=task_id,
                payload={
                    "output": output[:1000],
                    "pr_url": task.pr_url or "",
                },
            ).to_redis(),
        )
        await r.aclose()

    except Exception as e:
        log.exception(f"PR review response failed for task #{task_id}")
        await transition_task(task_id, "blocked", f"Failed to address review: {e}")


async def handle_clarification_response(task_id: int, answer: str) -> None:
    """Resume a task after the user answered a clarification question.

    Sends the answer into the existing session and re-triggers the
    appropriate phase (planning or coding) based on the task's prior state.
    """
    task = await get_task(task_id)
    if not task:
        return

    if not task.repo_name:
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        return

    session_id = _session_id(task_id, task.created_at)
    workspace = os.path.join(WORKSPACES_DIR, f"task-{task_id}")

    log.info(f"Resuming task #{task_id} with clarification answer (session={session_id})")

    # Send the answer into the existing session — Claude picks up where it left off
    output = await run_claude_code(
        workspace,
        f"The user answered your clarification question:\n\n{answer}\n\nPlease continue with the task.",
        timeout=900,
        session_id=session_id,
        resume=True,
    )

    # Check if Claude needs yet another clarification
    follow_up = _extract_clarification(output)
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

    # Clarification resolved — tell orchestrator to resume the task's previous phase
    r = await get_redis()
    await publish_event(
        r,
        Event(
            type="task.clarification_resolved",
            task_id=task_id,
            payload={"output": output},
        ).to_redis(),
    )
    await r.aclose()


DEPLOY_WORKFLOW_NAMES = ["deploy.yml", "deploy-dev.yml"]

DEPLOY_SCRIPT_CANDIDATES = [
    "scripts/deploy-dev.sh",
    "scripts/deploy-dev",
    "scripts/deploy_dev.sh",
    "scripts/deploy.sh",
    "deploy-dev.sh",
    "deploy.sh",
]


async def handle_deploy_preview(task_id: int) -> None:
    """Deploy the task's branch to a dev environment.

    Strategy (in order):
    1. Trigger a GitHub Actions deploy workflow (deploy.yml / deploy-dev.yml)
       via workflow_dispatch with environment=dev on the task branch.
    2. Fall back to running a local deploy script if found in the repo.
    3. Skip if neither is available.
    """
    task = await get_task(task_id)
    if not task or not task.repo_name:
        return

    branch_name = f"auto-agent/task-{task_id}"

    # Strategy 1: Try GitHub Actions workflow_dispatch
    if task.pr_url and settings.github_token:
        deployed = await _try_github_workflow_deploy(task_id, task, branch_name)
        if deployed:
            return

    # Strategy 2: Try local deploy script
    workspace = os.path.join(WORKSPACES_DIR, f"task-{task_id}")
    if not os.path.exists(workspace):
        log.info(f"Task #{task_id}: no workspace for deploy preview, skipping")
        return

    await _try_local_deploy(task_id, task, branch_name, workspace)


async def _try_github_workflow_deploy(task_id: int, task: TaskData, branch_name: str) -> bool:
    """Trigger a GitHub Actions deploy workflow via workflow_dispatch.

    Returns True if successfully triggered, False otherwise.
    """
    # Parse owner/repo from PR URL
    parts = task.pr_url.rstrip("/").split("/")
    owner, repo = parts[-4], parts[-3]

    headers = {
        "Authorization": f"token {settings.github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient() as client:
        # Find a deploy workflow
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows",
            headers=headers,
        )
        if resp.status_code != 200:
            log.warning(f"Task #{task_id}: failed to list workflows: {resp.status_code}")
            return False

        workflows = resp.json().get("workflows", [])
        deploy_workflow = None
        for wf in workflows:
            wf_filename = wf.get("path", "").split("/")[-1]
            if wf_filename in DEPLOY_WORKFLOW_NAMES and wf.get("state") == "active":
                deploy_workflow = wf
                break

        if not deploy_workflow:
            log.info(f"Task #{task_id}: no deploy workflow found in {owner}/{repo}")
            return False

        # Trigger workflow_dispatch on the task branch
        workflow_id = deploy_workflow["id"]
        log.info(
            f"Task #{task_id}: triggering workflow '{deploy_workflow['name']}' "
            f"on branch '{branch_name}' with environment=dev"
        )

        resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            headers=headers,
            json={
                "ref": branch_name,
                "inputs": {"environment": "dev"},
            },
        )

        if resp.status_code == 204:
            log.info(f"Task #{task_id}: deploy workflow triggered, waiting for completion...")

            # Poll for workflow run completion
            conclusion = await _wait_for_workflow_run(
                owner, repo, workflow_id, branch_name, headers, task_id,
            )

            r = await get_redis()
            if conclusion == "success":
                await publish_event(
                    r,
                    Event(
                        type="task.dev_deployed",
                        task_id=task_id,
                        payload={
                            "branch": branch_name,
                            "output": f"Deploy workflow '{deploy_workflow['name']}' completed successfully. Branch '{branch_name}' is now live on dev.",
                            "pr_url": task.pr_url or "",
                        },
                    ).to_redis(),
                )
            else:
                await publish_event(
                    r,
                    Event(
                        type="task.dev_deploy_failed",
                        task_id=task_id,
                        payload={
                            "branch": branch_name,
                            "output": f"Deploy workflow '{deploy_workflow['name']}' finished with conclusion: {conclusion}",
                            "pr_url": task.pr_url or "",
                        },
                    ).to_redis(),
                )
            await r.aclose()
            return True
        else:
            log.warning(
                f"Task #{task_id}: workflow dispatch failed: {resp.status_code} {resp.text[:200]}"
            )
            return False


async def _wait_for_workflow_run(
    owner: str, repo: str, workflow_id: int, branch: str,
    headers: dict, task_id: int,
    poll_interval: int = 30, max_wait: int = 1200,
) -> str:
    """Poll GitHub API until the workflow run completes. Returns conclusion string."""
    import time
    start = time.monotonic()

    # Wait a moment for the run to appear
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
                    status = run.get("status")
                    conclusion = run.get("conclusion")

                    if status == "completed":
                        log.info(f"Task #{task_id}: deploy workflow completed: {conclusion}")
                        return conclusion or "unknown"

                    log.info(f"Task #{task_id}: deploy workflow status: {status}, waiting...")

            await asyncio.sleep(poll_interval)

    log.warning(f"Task #{task_id}: deploy workflow timed out after {max_wait}s")
    return "timed_out"


async def _try_local_deploy(task_id: int, task: TaskData, branch_name: str, workspace: str) -> None:
    """Try running a local deploy script from the workspace."""
    deploy_script = None
    for candidate in DEPLOY_SCRIPT_CANDIDATES:
        script_path = os.path.join(workspace, candidate)
        if os.path.isfile(script_path):
            deploy_script = candidate
            break

    # Also check Makefile for deploy-dev target
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
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "BRANCH": branch_name, "TASK_ID": str(task_id)},
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "make", "deploy-dev",
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "BRANCH": branch_name, "TASK_ID": str(task_id)},
            )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning(f"Task #{task_id}: deploy script timed out after 300s")
            return

        stdout_str = (stdout or b"").decode()
        stderr_str = (stderr or b"").decode()
        output = (stdout_str + stderr_str).strip()

        if proc.returncode == 0:
            log.info(f"Task #{task_id}: dev deploy succeeded")
            r = await get_redis()
            await publish_event(
                r,
                Event(
                    type="task.dev_deployed",
                    task_id=task_id,
                    payload={
                        "branch": branch_name,
                        "output": output[-1000:],
                        "pr_url": task.pr_url or "",
                    },
                ).to_redis(),
            )
            await r.aclose()
        else:
            log.warning(f"Task #{task_id}: deploy script failed (exit {proc.returncode}): {output[-500:]}")

    except Exception:
        log.exception(f"Task #{task_id}: deploy preview failed")


async def handle_task_cleanup(task_id: int) -> None:
    """Clean up workspace and session for a finished task (done/failed)."""
    log.info(f"Cleaning up workspace for task #{task_id}")
    cleanup_workspace(task_id)


async def event_loop() -> None:
    """Main loop — listen for planning, coding, cleanup, and PR review events."""
    r = await get_redis()
    await ensure_stream_group(r)
    log.info("Claude runner event loop started")

    backoff = 1
    max_backoff = 60

    while True:
        try:
            messages = await read_events(
                r, consumer="claude-runner", count=1, block=5000
            )
            backoff = 1  # Reset on success
            for msg_id, data in messages:
                try:
                    event = Event.from_redis(data)
                    if event.type == "task.start_planning" and event.task_id:
                        feedback = event.payload.get("feedback") if event.payload else None
                        await handle_planning(event.task_id, feedback=feedback)
                    elif event.type == "task.start_coding" and event.task_id:
                        retry_reason = event.payload.get("retry_reason")
                        await handle_coding(event.task_id, retry_reason=retry_reason)
                    elif event.type == "task.deploy_preview" and event.task_id:
                        await handle_deploy_preview(event.task_id)
                    elif event.type == "task.cleanup" and event.task_id:
                        await handle_task_cleanup(event.task_id)
                    elif event.type == "task.clarification_response" and event.task_id:
                        answer = event.payload.get("answer", "")
                        if answer:
                            await handle_clarification_response(event.task_id, answer)
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
                            # Route based on task status
                            if task.status == "awaiting_clarification":
                                await handle_clarification_response(task_id, comments)
                            elif task.status in ("pr_created", "awaiting_ci", "awaiting_review", "coding"):
                                # PR exists — treat as review feedback
                                await handle_pr_review_comments(task_id, comments)
                            else:
                                # Task is pre-PR (awaiting_approval, planning, etc.)
                                # Re-emit the plan so user can see it again
                                log.info(
                                    f"Message for task #{task_id} in status '{task.status}' — "
                                    f"not routing to Claude (no branch/PR yet)"
                                )
                                if task.plan:
                                    r2 = await get_redis()
                                    await publish_event(
                                        r2,
                                        Event(
                                            type="task.plan_ready",
                                            task_id=task_id,
                                            payload={"plan": task.plan},
                                        ).to_redis(),
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
