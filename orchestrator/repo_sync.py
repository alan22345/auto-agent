"""Sync repos from GitHub — auto-discovers all repos the token can access."""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.models import Repo
from orchestrator.ci_extractor import extract_ci_checks

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


async def sync_repos(session: AsyncSession) -> int:
    """Fetch all repos visible to the GitHub token and upsert into the DB.

    Returns the number of new repos added.

    New repos are attached to the lowest-ID organization in the DB — this is
    the single-tenant default (the "owner" org). Multi-tenant deployments
    should sync per-org via the per-org GitHub App installation instead of
    relying on this env-level token sweep.
    """
    from shared.github_auth import get_github_token
    from shared.models import Organization

    token = await get_github_token()
    if not token:
        log.warning("No GitHub auth configured, skipping repo sync")
        return 0

    # Resolve the default organization to attach new repos to.
    default_org_q = await session.execute(
        select(Organization).order_by(Organization.id).limit(1)
    )
    default_org = default_org_q.scalar_one_or_none()
    if default_org is None:
        log.warning("No organizations in DB, skipping repo sync")
        return 0
    default_org_id = default_org.id

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

    repos_from_gh: list[dict] = []
    page = 1

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(
                f"{GITHUB_API}/user/repos",
                headers=headers,
                params={"per_page": 100, "page": page, "sort": "updated"},
            )
            if resp.status_code != 200:
                log.error(f"GitHub API error: {resp.status_code} {resp.text[:200]}")
                break

            batch = resp.json()
            if not batch:
                break

            repos_from_gh.extend(batch)
            page += 1

    # Upsert into DB — insert new repos, update default_branch on existing ones
    added = 0
    for gh_repo in repos_from_gh:
        name = gh_repo["full_name"]  # e.g. "Ergodic/cardamon"
        clone_url = gh_repo["clone_url"]
        default_branch = gh_repo.get("default_branch", "main")

        result = await session.execute(select(Repo).where(Repo.name == name))
        existing = result.scalar_one_or_none()
        if existing:
            # Update URL in case it changed, but don't touch default_branch
            # — it may have been manually overridden via /branch command
            if existing.url != clone_url:
                existing.url = clone_url
            # Extract CI checks if not already cached
            if not existing.ci_checks:
                ci_checks = await extract_ci_checks(clone_url)
                if ci_checks:
                    existing.ci_checks = ci_checks
                    log.info(f"Extracted CI checks for '{name}'")
            continue

        # New repo — extract CI checks
        ci_checks = await extract_ci_checks(clone_url)
        repo = Repo(
            name=name,
            url=clone_url,
            default_branch=default_branch,
            ci_checks=ci_checks,
            organization_id=default_org_id,
        )
        session.add(repo)
        if ci_checks:
            log.info(f"Extracted CI checks for new repo '{name}'")
        added += 1

    # Also add/update short-name aliases (e.g. "cardamon" -> same URL)
    # so users can refer to repos by just the repo name
    for gh_repo in repos_from_gh:
        short_name = gh_repo["name"]  # e.g. "cardamon"
        default_branch = gh_repo.get("default_branch", "main")

        result = await session.execute(select(Repo).where(Repo.name == short_name))
        existing = result.scalar_one_or_none()
        if existing:
            # Copy CI checks from full-name entry if missing
            if not existing.ci_checks:
                full_result = await session.execute(
                    select(Repo).where(Repo.name == gh_repo["full_name"])
                )
                full_repo = full_result.scalar_one_or_none()
                if full_repo and full_repo.ci_checks:
                    existing.ci_checks = full_repo.ci_checks
            continue

        # Copy CI checks from the full-name entry we just created
        full_result = await session.execute(
            select(Repo).where(Repo.name == gh_repo["full_name"])
        )
        full_repo = full_result.scalar_one_or_none()
        ci_checks = full_repo.ci_checks if full_repo else None

        repo = Repo(
            name=short_name,
            url=gh_repo["clone_url"],
            default_branch=default_branch,
            ci_checks=ci_checks,
            organization_id=default_org_id,
        )
        session.add(repo)
        added += 1

    await session.commit()
    log.info(f"Repo sync complete: {len(repos_from_gh)} from GitHub, {added} new in DB")
    return added


async def match_repo(session: AsyncSession, text: str) -> Repo | None:
    """Try to find a repo name mentioned in the text.

    Uses two passes:
    1. Exact substring match (e.g. text contains "cardamon" → matches repo "cardamon")
    2. Fuzzy match on hyphenated segments — handles cases where the user says
       "iot-apartment-generator" but the repo is "iot-apartment-simulator".
       Requires at least 2 matching segments and >50% overlap.
    """
    text_lower = text.lower()

    result = await session.execute(select(Repo).order_by(Repo.name))
    repos = result.scalars().all()

    # Pass 1: exact substring match (longest names first)
    repos_sorted = sorted(repos, key=lambda r: len(r.name), reverse=True)
    for repo in repos_sorted:
        if repo.name.lower() in text_lower:
            return repo

    # Pass 2: fuzzy match on hyphenated segments
    text_words = set(text_lower.replace("/", " ").replace("-", " ").split())
    best_repo = None
    best_score = 0
    for repo in repos_sorted:
        # Get the short name (after the last /)
        short_name = repo.name.split("/")[-1].lower()
        segments = set(short_name.replace("-", " ").split())
        if len(segments) < 2:
            continue  # Skip single-word repos for fuzzy (too ambiguous)
        overlap = segments & text_words
        score = len(overlap) / len(segments) if segments else 0
        if len(overlap) >= 2 and score > best_score:
            best_score = score
            best_repo = repo

    # Require >50% segment overlap to avoid false matches
    if best_repo and best_score > 0.5:
        return best_repo

    return None
