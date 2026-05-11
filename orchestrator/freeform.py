"""Freeform mode operations — promote dev changes to main, revert from dev."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


async def promote_task_to_main(
    pr_url: str, branch_name: str, repo_url: str,
    *, user_id: int | None = None,
) -> str | None:
    """Promote a freeform task from dev to main by creating a new PR.

    The task's feature branch still exists on remote after the dev merge,
    so we create a new PR from it targeting main.

    Returns the new PR URL, or None on failure.
    """
    from shared.github_auth import get_github_token

    token = await get_github_token(user_id=user_id)
    if not pr_url or not token:
        return None

    parts = pr_url.rstrip("/").split("/")
    owner, repo = parts[-4], parts[-3]

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient() as client:
        # Get the original PR to extract details
        pr_number = parts[-1]
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        if resp.status_code != 200:
            log.warning(f"Failed to get original PR: {resp.status_code}")
            return None

        original = resp.json()
        title = original.get("title", "Promote to main")
        body = original.get("body", "")

        # Create new PR targeting main
        resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=headers,
            json={
                "title": f"[Promote] {title}",
                "body": f"Promoting from dev to main.\n\nOriginal PR: {pr_url}\n\n{body}",
                "head": branch_name,
                "base": "main",
            },
        )
        if resp.status_code in (200, 201):
            new_pr_url = resp.json().get("html_url", "")
            log.info(f"Promotion PR created: {new_pr_url}")
            return new_pr_url

        log.warning(f"Failed to create promotion PR: {resp.status_code} {resp.text[:200]}")
        return None


async def revert_task_from_dev(
    pr_url: str, dev_branch: str = "dev",
    *, user_id: int | None = None,
) -> str | None:
    """Revert a freeform task's merge commit from the dev branch.

    Finds the merge commit from the original PR and creates a revert PR.

    Returns the revert PR URL, or None on failure.
    """
    from shared.github_auth import get_github_token

    token = await get_github_token(user_id=user_id)
    if not pr_url or not token:
        return None

    parts = pr_url.rstrip("/").split("/")
    owner, repo, pr_number = parts[-4], parts[-3], parts[-1]

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient() as client:
        # Get the PR's merge commit SHA
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        if resp.status_code != 200:
            log.warning(f"Failed to get PR: {resp.status_code}")
            return None

        pr_data = resp.json()
        merge_commit_sha = pr_data.get("merge_commit_sha")
        if not merge_commit_sha:
            log.warning(f"PR #{pr_number} has no merge commit")
            return None

        title = pr_data.get("title", "Unknown")

        # Create a revert branch and PR via the GitHub API
        # First, get the dev branch ref
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{dev_branch}",
            headers=headers,
        )
        if resp.status_code != 200:
            log.warning(f"Failed to get dev branch ref: {resp.status_code}")
            return None

        revert_branch = f"revert/pr-{pr_number}"

        # Create the revert branch from dev
        dev_sha = resp.json()["object"]["sha"]
        resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{revert_branch}", "sha": dev_sha},
        )
        if resp.status_code not in (200, 201):
            # Branch may already exist — try updating it
            resp = await client.patch(
                f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{revert_branch}",
                headers=headers,
                json={"sha": dev_sha, "force": True},
            )

        # Create a revert PR (GitHub doesn't have a direct revert API for branches,
        # but we can request a revert of the merge commit)
        resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=headers,
            json={
                "title": f"[Revert] {title}",
                "body": f"Reverting {pr_url} (merge commit: {merge_commit_sha[:8]})",
                "head": revert_branch,
                "base": dev_branch,
            },
        )

        if resp.status_code in (200, 201):
            revert_pr_url = resp.json().get("html_url", "")
            log.info(f"Revert PR created: {revert_pr_url}")
            return revert_pr_url

        log.warning(f"Failed to create revert PR: {resp.status_code} {resp.text[:200]}")
        return None
