"""Review phase — independent PR review, plan auto-review, and PR comment handling.

Three handlers and the gh-CLI PR creation helpers live here together because
they share concerns: PR creation idempotency, the freeform plan auto-review
hook, and the human-driven comment-resume flow all touch the same review
session conventions and the same independent-reviewer guardrails.
"""

from __future__ import annotations

import tempfile

import httpx

from agent import sh
from agent.lifecycle._naming import _branch_name, _fresh_session_id, _session_id
from agent.lifecycle._orchestrator_api import (
    ORCHESTRATOR_URL,
    get_freeform_config,
    get_repo,
    get_task,
    transition_task,
)
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.prompts import (
    build_plan_independent_review_prompt,
    build_pr_independent_review_prompt,
    build_pr_review_response_prompt,
)
from agent.workspace import (
    clone_repo,
    commit_pending_changes,
    push_branch,
)
from shared.events import (
    Event,
    publish,
    task_review_comments_addressed,
    task_review_complete,
)
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.review")


async def find_existing_pr_url(
    workspace: str,
    head_branch: str,
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
) -> str | None:
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
    from shared.github_auth import get_github_token

    result = await sh.run(
        ["gh", "pr", "list", "--head", head_branch, "--state", "open", "--json", "url,state"],
        cwd=workspace,
        timeout=20,
        env={"GH_TOKEN": await get_github_token(
            user_id=user_id, organization_id=organization_id,
        )},
    )
    if result.failed:
        return None
    try:
        import json as _json

        prs = _json.loads(result.stdout)
        for pr in prs:
            if pr.get("state") == "OPEN" and pr.get("url"):
                return pr["url"]
    except Exception:
        return None
    return None


async def create_pr(
    workspace: str,
    title: str,
    body: str,
    base_branch: str,
    head_branch: str,
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
) -> str:
    """Create a PR using the gh CLI, or return the existing one if the branch
    already has an open PR. Idempotent — safe to call after pushing new
    commits to a branch with an existing PR."""
    existing = await find_existing_pr_url(
        workspace, head_branch,
        user_id=user_id, organization_id=organization_id,
    )
    if existing:
        log.info(f"PR already exists for {head_branch}, reusing: {existing}")
        return existing

    from shared.github_auth import get_github_token

    result = await sh.run(
        [
            "gh", "pr", "create",
            "--title", title,
            "--body", body,
            "--base", base_branch,
            "--head", head_branch,
        ],
        cwd=workspace,
        timeout=30,
        env={"GH_TOKEN": await get_github_token(
            user_id=user_id, organization_id=organization_id,
        )},
    )
    if result.failed:
        raise RuntimeError(
            f"gh pr create failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


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

    # Per-invocation session — the reviewer is a fresh, independent agent
    # by design. A deterministic hash here collides on retry (the Claude
    # CLI provider rejects re-used session IDs with "already in use").
    reviewer_session = _fresh_session_id(task_id, "review")

    log.info(f"Independent review of task #{task_id} PR (session={reviewer_session})")
    workspace = await clone_repo(
        repo.url, task_id, base_branch,
        fallback_branch=fallback_branch,
        user_id=task.created_by_user_id,
        organization_id=task.organization_id,
    )

    try:
        await sh.run(["git", "checkout", branch_name], cwd=workspace, timeout=30)

        prompt = build_pr_independent_review_prompt(
            task.title, task.description, pr_url, base_branch
        )
        agent = create_agent(
            workspace,
            session_id=reviewer_session,
            readonly=True,
            max_turns=20,
            task_description=task.description,
            repo_name=task.repo_name,
            home_dir=await home_dir_for_task(task),
        )
        result = await agent.run(prompt)
        output = result.output
        log.info(f"Independent review for task #{task_id}: {output[:300]}...")

        # If the underlying provider crashed (e.g. Claude CLI subprocess
        # failure that didn't recover), it returns "[ERROR] ...". Treating
        # that string as review feedback would route it to the coding agent
        # as if it were a real comment — and the coder would try to "fix"
        # the CLI error. Skip the review entirely on a recognised error
        # prefix and emit an auto-approve so the task isn't blocked.
        if output.lstrip().startswith("[ERROR]"):
            log.warning(
                f"Task #{task_id}: reviewer agent errored ({output[:200]!r}), "
                "skipping review and auto-approving so the task isn't blocked"
            )
            await publish(
                task_review_complete(
                    task_id,
                    review=f"Review skipped — agent error: {output[:500]}",
                    pr_url=pr_url,
                    branch=branch_name,
                    approved=True,
                )
            )
            return

        approved = any(
            phrase in output.lower()
            for phrase in ["--approve", "lgtm", "looks good", "pr review --approve"]
        )

        if approved:
            log.info(f"Independent review approved task #{task_id}")
            await publish(
                task_review_complete(
                    task_id,
                    review=output[:2000],
                    pr_url=pr_url,
                    branch=branch_name,
                    approved=True,
                )
            )
        else:
            log.info(f"Independent review requested changes for task #{task_id}")
            session_id = _session_id(task_id, task.created_at)
            fix_prompt = (
                f"An independent code reviewer left feedback on your PR. "
                f"Address their comments:\n\n{output}\n\nFix the issues, commit, and push."
            )
            fix_agent = create_agent(
                workspace,
                session_id=session_id,
                max_turns=30,
                task_description=task.description,
                repo_name=task.repo_name,
                home_dir=await home_dir_for_task(task),
            )
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

            await publish(
                task_review_complete(
                    task_id,
                    review=output[:2000],
                    fixes=fix_result.output[:1000],
                    pr_url=pr_url,
                    branch=branch_name,
                    approved=False,
                )
            )

    except Exception as e:
        log.exception(f"Independent review failed for task #{task_id}")
        await publish(
            task_review_complete(
                task_id,
                review=f"Review skipped: {e}",
                pr_url=pr_url,
                branch=branch_name,
                approved=True,
            )
        )


async def handle_plan_independent_review(task_id: int) -> None:
    """Run an independent reviewer on a freeform task's plan."""
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
                json={
                    "approved": True,
                    "message": "Plan auto-approved (plan text was empty — review skipped)",
                },
            )
        return

    log.info(f"Running independent plan review for freeform task #{task_id}")
    prompt = build_plan_independent_review_prompt(task.title, task.description, task.plan)

    try:
        with tempfile.TemporaryDirectory(prefix=f"plan-review-{task_id}-") as tmp:
            agent = create_agent(
                tmp,
                readonly=True,
                max_turns=5,
                model_tier="fast",
                task_description=task.description,
                repo_name=task.repo_name,
                home_dir=await home_dir_for_task(task),
            )
            result = await agent.run(prompt)
            output = result.output
    except Exception as e:
        log.exception(f"Plan auto-review failed for task #{task_id}")
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
                json={
                    "approved": True,
                    "message": f"Plan auto-approved (reviewer error: {e})",
                },
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
    reasoning = (
        "\n".join(output_stripped.splitlines()[reasoning_start:]).strip()
        or "(no reasoning provided)"
    )

    approved = verdict.startswith("APPROVE")
    decision_label = "APPROVED" if approved else "REJECTED"
    log_message = f"Plan {decision_label} by independent reviewer\n\n{reasoning[:1900]}"

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/approve",
            json={
                "approved": approved,
                "feedback": reasoning if not approved else "",
                "message": log_message,
            },
        )

    log.info(f"Plan auto-review complete for task #{task_id}: {decision_label}")


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
    workspace = await clone_repo(
        repo.url, task_id, base_branch,
        user_id=task.created_by_user_id,
        organization_id=task.organization_id,
    )

    try:
        await sh.run(["git", "checkout", branch_name], cwd=workspace, timeout=30)

        prompt = build_pr_review_response_prompt(task.title, task.description, comments)
        agent = create_agent(
            workspace,
            session_id=session_id,
            max_turns=30,
            task_description=task.description,
            repo_name=task.repo_name,
            home_dir=await home_dir_for_task(task),
        )
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

        await publish(
            task_review_comments_addressed(
                task_id, output=result.output[:1000], pr_url=task.pr_url or ""
            )
        )

    except Exception as e:
        log.exception(f"PR review response failed for task #{task_id}")
        await transition_task(task_id, "blocked", f"Failed to address review: {e}")


async def handle_plan_ready(event: Event) -> None:
    """EventBus entry — auto-review the plan for freeform-mode tasks.

    Guards on ``task.freeform_mode`` and ``status == "awaiting_approval"``
    inside ``handle_plan_independent_review``; this handler just dispatches.
    """
    if not event.task_id:
        return
    task = await get_task(event.task_id)
    if task and task.freeform_mode and task.status == "awaiting_approval":
        await handle_plan_independent_review(event.task_id)
