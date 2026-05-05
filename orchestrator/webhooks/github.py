"""GitHub webhook handler — receives CI status, PR reviews, and merge events.

Configure in GitHub repo settings:
  Payload URL: https://<your-domain>/api/webhooks/github
  Content type: application/json
  Secret: <GITHUB_WEBHOOK_SECRET from .env>
  Events: Check suites, Pull request reviews, Pull requests
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.database import async_session
from shared.events import (
    human_message,
    publish,
    task_ci_failed,
    task_ci_passed,
    task_failed,
    task_lgtm_received,
    task_review_approved,
)
from shared.models import Task, TaskStatus
from shared.types import PRReviewComment

log = logging.getLogger(__name__)

router = APIRouter()


_webhook_secret_warned = False


def _verify_signature(payload: bytes, signature: str) -> bool:
    """Verify GitHub HMAC-SHA256 signature."""
    global _webhook_secret_warned
    if not settings.github_webhook_secret:
        if not _webhook_secret_warned:
            log.warning(
                "GITHUB_WEBHOOK_SECRET is not set — webhook signature verification is disabled. "
                "Set this in production to prevent spoofed webhooks."
            )
            _webhook_secret_warned = True
        return True
    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _find_task_by_pr_url(session: AsyncSession, pr_url: str) -> Task | None:
    """Look up the task that owns this PR."""
    result = await session.execute(
        select(Task).where(Task.pr_url == pr_url)
    )
    return result.scalar_one_or_none()


async def _find_task_by_branch(session: AsyncSession, branch: str) -> Task | None:
    """Look up a task by its branch name."""
    result = await session.execute(
        select(Task).where(Task.branch_name == branch)
    )
    return result.scalar_one_or_none()


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(""),
    x_github_event: str = Header(""),
) -> dict[str, str]:
    """Handle incoming GitHub webhook events."""
    body = await request.body()

    if not _verify_signature(body, x_hub_signature_256):
        raise HTTPException(403, "Invalid signature")

    payload: dict[str, Any] = await request.json()

    if x_github_event == "check_suite":
        await _handle_check_suite(payload)
    elif x_github_event == "pull_request_review":
        await _handle_pr_review(payload)
    elif x_github_event == "pull_request_review_comment":
        await _handle_pr_comment(payload)
    elif x_github_event == "issue_comment":
        await _handle_issue_comment(payload)
    elif x_github_event == "pull_request":
        await _handle_pull_request(payload)
    elif x_github_event == "status":
        await _handle_commit_status(payload)
    else:
        log.debug(f"Ignoring GitHub event: {x_github_event}")

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Check suite / CI status
# ---------------------------------------------------------------------------


async def _handle_check_suite(payload: dict[str, Any]) -> None:
    """Handle check_suite completed — CI passed or failed."""
    action = payload.get("action")
    if action != "completed":
        return

    check_suite = payload.get("check_suite", {})
    conclusion = check_suite.get("conclusion")  # success, failure, etc.
    head_branch: str = check_suite.get("head_branch", "")

    if not head_branch.startswith("auto-agent/"):
        return  # Not one of our branches

    async with async_session() as session:
        task = await _find_task_by_branch(session, head_branch)
        if not task:
            return
        if task.status != TaskStatus.AWAITING_CI:
            return  # Not waiting for CI

        if conclusion == "success":
            await publish(task_ci_passed(task.id))
            log.info(f"CI passed for task #{task.id} (webhook)")
        elif conclusion in ("failure", "timed_out", "action_required"):
            await publish(task_ci_failed(task.id, reason=f"CI conclusion: {conclusion}"))
            log.info(f"CI failed for task #{task.id}: {conclusion} (webhook)")


async def _handle_commit_status(payload: dict[str, Any]) -> None:
    """Handle commit status events (some repos use status API instead of check suites)."""
    state = payload.get("state")  # success, failure, pending, error
    branches = payload.get("branches", [])

    for branch_info in branches:
        branch_name: str = branch_info.get("name", "")
        if not branch_name.startswith("auto-agent/"):
            continue

        async with async_session() as session:
            task = await _find_task_by_branch(session, branch_name)
            if not task or task.status != TaskStatus.AWAITING_CI:
                continue

            if state == "success":
                await publish(task_ci_passed(task.id))
                log.info(f"CI passed for task #{task.id} (status webhook)")
            elif state in ("failure", "error"):
                await publish(task_ci_failed(task.id, reason=f"Commit status: {state}"))
                log.info(f"CI failed for task #{task.id}: {state} (status webhook)")


# ---------------------------------------------------------------------------
# PR reviews
# ---------------------------------------------------------------------------


async def _handle_pr_review(payload: dict[str, Any]) -> None:
    """Handle pull_request_review submitted — review comments from humans."""
    action = payload.get("action")
    if action != "submitted":
        return

    review = payload.get("review", {})
    review_state = review.get("state", "")  # approved, changes_requested, commented
    pr = payload.get("pull_request", {})
    pr_url: str = pr.get("html_url", "")
    head_branch: str = pr.get("head", {}).get("ref", "")

    if not head_branch.startswith("auto-agent/"):
        return

    async with async_session() as session:
        task = await _find_task_by_pr_url(session, pr_url)
        if not task:
            task = await _find_task_by_branch(session, head_branch)
        if not task:
            return

        if review_state == "approved":
            reviewer = review.get("user", {}).get("login", "unknown")
            await publish(task_lgtm_received(task.id, reviewer=reviewer, pr_url=pr_url))
            log.info(f"LGTM (review approved) on task #{task.id} by {reviewer} (webhook)")

        elif review_state == "changes_requested":
            comment = PRReviewComment(
                author=review.get("user", {}).get("login", "unknown"),
                body=review.get("body", ""),
                type="review",
            )
            await publish(
                human_message(
                    task_id=task.id,
                    message=f"[{comment.author}] Review (changes requested): {comment.body}",
                    source="github_review",
                )
            )
            log.info(f"Changes requested on task #{task.id} by {comment.author} (webhook)")


# ---------------------------------------------------------------------------
# PR comments (line comments and conversation comments)
# ---------------------------------------------------------------------------


async def _handle_pr_comment(payload: dict[str, Any]) -> None:
    """Handle pull_request_review_comment — inline code comments on a PR."""
    action = payload.get("action")
    if action != "created":
        return

    comment = payload.get("comment", {})
    pr = payload.get("pull_request", {})
    pr_url: str = pr.get("html_url", "")
    head_branch: str = pr.get("head", {}).get("ref", "")
    author: str = comment.get("user", {}).get("login", "unknown")
    body: str = comment.get("body", "")
    path: str = comment.get("path", "")

    if not head_branch.startswith("auto-agent/"):
        return
    # Ignore comments from bots (including our own)
    if comment.get("user", {}).get("type") == "Bot":
        return

    async with async_session() as session:
        task = await _find_task_by_pr_url(session, pr_url)
        if not task:
            task = await _find_task_by_branch(session, head_branch)
        if not task:
            return

        file_context = f" on `{path}`" if path else ""
        await publish(
            human_message(
                task_id=task.id,
                message=f"[{author}] PR comment{file_context}: {body}",
                source="github_pr_comment",
            )
        )
        log.info(f"PR comment on task #{task.id} by {author} (webhook)")


async def _handle_issue_comment(payload: dict[str, Any]) -> None:
    """Handle issue_comment — conversation comments on a PR (not inline)."""
    action = payload.get("action")
    if action != "created":
        return

    # Only handle comments on PRs (issues with pull_request key)
    issue = payload.get("issue", {})
    if "pull_request" not in issue:
        return

    comment = payload.get("comment", {})
    author: str = comment.get("user", {}).get("login", "unknown")
    body: str = comment.get("body", "")
    pr_url: str = issue.get("pull_request", {}).get("html_url", "")

    # Ignore bot comments
    if comment.get("user", {}).get("type") == "Bot":
        return

    if not pr_url:
        return

    async with async_session() as session:
        task = await _find_task_by_pr_url(session, pr_url)
        if not task:
            return

        await publish(
            human_message(
                task_id=task.id,
                message=f"[{author}] PR comment: {body}",
                source="github_pr_comment",
            )
        )
        log.info(f"PR conversation comment on task #{task.id} by {author} (webhook)")


# ---------------------------------------------------------------------------
# PR merged / closed
# ---------------------------------------------------------------------------


async def _handle_pull_request(payload: dict[str, Any]) -> None:
    """Handle pull_request closed+merged — task is done. Also detects CI failure on main."""
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    pr_url: str = pr.get("html_url", "")
    head_branch: str = pr.get("head", {}).get("ref", "")
    merged: bool = pr.get("merged", False)

    if action == "closed" and merged and head_branch.startswith("auto-agent/"):
        async with async_session() as session:
            task = await _find_task_by_pr_url(session, pr_url)
            if not task:
                task = await _find_task_by_branch(session, head_branch)
            if not task:
                return

            await publish(task_review_approved(task.id))
            log.info(f"PR merged for task #{task.id} (webhook)")

    elif action == "closed" and not merged and head_branch.startswith("auto-agent/"):
        # PR was closed without merging — task failed / rejected
        async with async_session() as session:
            task = await _find_task_by_pr_url(session, pr_url)
            if not task:
                task = await _find_task_by_branch(session, head_branch)
            if not task:
                return

            await publish(task_failed(task.id, error="PR closed without merging"))
            log.info(f"PR closed without merge for task #{task.id} (webhook)")
