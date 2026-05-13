"""Create a brand-new GitHub repo from a natural-language description.

Flow:
1. Ask Claude for a short repo slug given the description.
2. POST to GitHub to create the repo (auto_init=true so it has a main branch).
3. Insert Repo + FreeformConfig + scaffold Task into the DB.
4. Publish task.created so the existing pipeline picks it up.

The scaffold task runs through the normal classification → planning → coding
pipeline, but with freeform_mode=True so:
  - The plan (if generated) is auto-approved by an independent reviewer.
  - The PR auto-merges to main once CI passes (no human review).
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.llm import get_provider
from agent.llm.types import Message
from agent.prompts import build_repo_name_prompt
from shared.config import settings
from shared.events import publish, task_created
from shared.models import FreeformConfig, Repo, Task, TaskComplexity, TaskSource, TaskStatus

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class CreateRepoError(Exception):
    pass


def _slug_fallback(description: str) -> str:
    """Deterministic fallback if Claude name generation fails."""
    text = re.sub(r"[^a-z0-9\s-]", " ", description.lower())
    words = [w for w in text.split() if len(w) > 2][:4]
    if not words:
        words = ["new-project"]
    slug = "-".join(words)[:40].strip("-")
    return slug or "new-project"


def _sanitize_slug(name: str) -> str:
    """Force any name into a valid GitHub repo slug."""
    name = name.strip().lower()
    # Take only the first line in case Claude added explanation
    name = name.splitlines()[0].strip()
    # Strip quotes/backticks/punctuation
    name = name.strip("`'\"., ")
    # Replace any non-allowed chars with hyphens
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:40] or "new-project"


async def _generate_name_via_claude(description: str) -> str:
    """Ask the configured LLM provider for a short repo slug."""
    prompt = build_repo_name_prompt(description)
    try:
        # "fast" tier (Haiku) — naming is the canonical fast-tier use case
        # per agent/llm/__init__.py::MODEL_TIERS.
        provider = get_provider(model_override="fast")
        response = await provider.complete(
            messages=[Message(role="user", content=prompt)],
            max_tokens=50,
        )
        output = response.message.content
    except Exception:
        log.exception("Claude name generation failed, using fallback")
        return _slug_fallback(description)

    name = _sanitize_slug(output)
    if not name or name == "new-project":
        return _slug_fallback(description)
    return name


async def _resolve_owner(
    client: httpx.AsyncClient, override: str = "",
    *, user_id: int | None = None,
) -> tuple[str, bool]:
    """Return (owner_login, is_org). Tries override → settings → GET /user."""
    from shared.github_auth import get_github_token

    headers = {
        "Authorization": f"token {await get_github_token(user_id=user_id)}",
        "Accept": "application/vnd.github+json",
    }
    candidate = override or settings.github_owner

    if candidate:
        # Try as org first; if 404, treat as user
        resp = await client.get(f"{GITHUB_API}/orgs/{candidate}", headers=headers)
        if resp.status_code == 200:
            return candidate, True
        return candidate, False

    resp = await client.get(f"{GITHUB_API}/user", headers=headers)
    if resp.status_code != 200:
        raise CreateRepoError(
            f"Could not look up token's user (HTTP {resp.status_code}). "
            f"Set GITHUB_OWNER explicitly."
        )
    return resp.json()["login"], False


async def _create_github_repo(
    client: httpx.AsyncClient,
    name: str,
    description: str,
    owner: str,
    is_org: bool,
    private: bool,
    *,
    user_id: int | None = None,
) -> dict:
    from shared.github_auth import get_github_token

    headers = {
        "Authorization": f"token {await get_github_token(user_id=user_id)}",
        "Accept": "application/vnd.github+json",
    }
    url = f"{GITHUB_API}/orgs/{owner}/repos" if is_org else f"{GITHUB_API}/user/repos"
    payload = {
        "name": name,
        "description": description[:350],
        "private": private,
        "auto_init": True,
    }
    resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code == 422:
        raise CreateRepoError(
            f"GitHub rejected the repo name '{name}' — "
            f"it probably already exists. Try a different description."
        )
    if resp.status_code == 403:
        raise CreateRepoError(
            "GitHub returned 403 — your token lacks permission to create repos. "
            "Make sure it has the 'repo' scope (and 'admin:org' if creating under an org)."
        )
    if resp.status_code not in (200, 201):
        raise CreateRepoError(
            f"GitHub repo creation failed: {resp.status_code} {resp.text[:300]}"
        )
    return resp.json()


async def create_repo_and_scaffold_task(
    session: AsyncSession,
    description: str,
    org_override: str = "",
    private: bool = True,
    loop: bool = True,
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
) -> tuple[Repo, Task]:
    """End-to-end: pick a name, create the GitHub repo, register it, queue a scaffold task."""
    from shared.github_auth import get_github_token

    if not await get_github_token(user_id=user_id, organization_id=organization_id):
        raise CreateRepoError(
            "No GitHub auth configured — set GITHUB_TOKEN, or configure a "
            "GitHub App via GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY + "
            "GITHUB_APP_INSTALLATION_ID."
        )

    description = description.strip()
    if not description:
        raise CreateRepoError("Description is required.")

    # 1. Name generation
    name = await _generate_name_via_claude(description)
    log.info(f"Generated repo name '{name}' for description: {description[:80]}")

    async with httpx.AsyncClient(timeout=30) as client:
        # 2. Resolve owner
        owner, is_org = await _resolve_owner(client, org_override, user_id=user_id)
        log.info(f"Creating repo {owner}/{name} (is_org={is_org}, private={private})")

        # 3. Create on GitHub
        gh_repo = await _create_github_repo(
            client, name, description, owner, is_org, private,
            user_id=user_id,
        )

    full_name = gh_repo["full_name"]
    clone_url = gh_repo["clone_url"]
    default_branch = gh_repo.get("default_branch", "main")

    # Give GitHub a moment to finish provisioning before the clone happens.
    await asyncio.sleep(2)

    # 4. Insert Repo rows (full name + short alias, mirroring repo_sync.py)
    full_repo = Repo(
        name=full_name, url=clone_url, default_branch=default_branch,
        organization_id=organization_id,
    )
    session.add(full_repo)

    # Short alias only if not colliding *within the caller's org*
    short_repo = None
    short_lookup_q = select(Repo).where(Repo.name == name)
    if organization_id is not None:
        short_lookup_q = short_lookup_q.where(Repo.organization_id == organization_id)
    existing_short = await session.execute(short_lookup_q)
    if existing_short.scalar_one_or_none() is None:
        short_repo = Repo(
            name=name, url=clone_url, default_branch=default_branch,
            organization_id=organization_id,
        )
        session.add(short_repo)

    await session.flush()  # populate IDs
    primary_repo = short_repo or full_repo

    # 5. Enable freeform mode for this repo. The `loop` flag controls whether
    # the continuous-improvement loop runs autonomously:
    #   loop=True  -> every 30 min PO analysis + auto-approval of suggestions
    #   loop=False -> weekly PO analysis, suggestions sit in PENDING for the user
    # For a fresh repo, prod and dev both start as the default branch —
    # the orchestrator will create a separate dev branch on first freeform
    # task if the user later configures one.
    config = FreeformConfig(
        repo_id=primary_repo.id,
        enabled=True,
        prod_branch=default_branch,
        dev_branch=default_branch,
        analysis_cron="*/30 * * * *" if loop else "0 9 * * 1",
        auto_approve_suggestions=loop,
        organization_id=organization_id,
    )
    session.add(config)

    # 6. Create the scaffold task
    title = description.splitlines()[0][:120]
    if not title.lower().startswith(("scaffold", "build", "create")):
        title = f"Scaffold: {title}"

    scaffold_description = (
        f"This is a brand-new empty repository. Scaffold the initial codebase from scratch.\n\n"
        f"## What the user wants\n{description}\n\n"
        f"## Instructions\n"
        f"1. Pick an appropriate tech stack and project structure for what the user described.\n"
        f"2. Initialize the project: package manifests, .gitignore, README, and any setup files needed.\n"
        f"3. Implement a working first version that demonstrates the core functionality end-to-end.\n"
        f"4. Commit your work with a clear message.\n"
        f"\n"
        f"The repo currently only contains an auto-generated README. Feel free to overwrite it."
    )

    task = Task(
        title=title,
        description=scaffold_description,
        source=TaskSource.MANUAL,
        status=TaskStatus.INTAKE,
        # Pre-classify scaffold tasks as complex_large so they always go
        # through the trio pipeline (architect → builder → reviewer)
        # regardless of what the keyword classifier might guess from the
        # description text.
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=primary_repo.id,
        freeform_mode=True,
        organization_id=organization_id,
        created_by_user_id=user_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    await session.refresh(primary_repo)

    # 7. Publish task.created so the orchestrator picks it up
    await publish(task_created(task.id))

    log.info(f"Scaffold task #{task.id} created for repo {full_name}")
    return primary_repo, task
