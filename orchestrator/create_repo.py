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


def _sanitize_description(text: str) -> str:
    """Make a description safe for GitHub's repo `description` field.

    GitHub rejects descriptions with control characters (newlines, tabs).
    Replace them with spaces and collapse runs of whitespace.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:350]


_TITLE_SKIP_PREFIXES: tuple[str, ...] = ("#", ">", "```", "|", "---", "===")
_TITLE_NUMBERED_RE = re.compile(r"^\d+[.)]")


def _first_prose_line(description: str) -> str:
    """Return the first non-structural line of ``description``, or ``""``.

    Skips empty lines, markdown headers (``#``, ``##``…), blockquotes,
    code fences, table rows, horizontal rules, and numbered section
    markers (``1.``, ``2)``). The intent-grill agent reads the task title
    verbatim; if it sees ``## 1. …`` it infers a "section of a series"
    framing and under-scopes the whole build (ADR-018 regression observed
    on the first harpoon attempt).
    """

    for raw in description.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(_TITLE_SKIP_PREFIXES):
            continue
        if _TITLE_NUMBERED_RE.match(line):
            continue
        return line
    return ""


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
        "description": _sanitize_description(description),
        "private": private,
        "auto_init": True,
    }
    resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code == 403:
        raise CreateRepoError(
            "GitHub returned 403 — your token lacks permission to create repos. "
            "Make sure it has the 'repo' scope (and 'admin:org' if creating under an org)."
        )
    if resp.status_code not in (200, 201):
        # Surface GitHub's actual validation errors instead of guessing.
        # 422 from /user/repos can mean "name taken", "name reserved after
        # deletion", "name violates rules", "free-plan private-repo limit",
        # etc. — different fixes for each.
        target = f"{owner}/{name}" if is_org else name
        try:
            body = resp.json()
            top = body.get("message", "")
            errs = body.get("errors", []) or []
            parts: list[str] = []
            for err in errs:
                msg = err.get("message") or err.get("code")
                field = err.get("field")
                if msg and field:
                    parts.append(f"{field}: {msg}")
                elif msg:
                    parts.append(msg)
            detail = "; ".join(parts) if parts else top
        except (ValueError, AttributeError):
            detail = resp.text[:300]
        raise CreateRepoError(
            f"GitHub rejected the request for '{target}' "
            f"(HTTP {resp.status_code}): {detail}"
        )
    return resp.json()


async def create_repo_and_scaffold_task(
    session: AsyncSession,
    description: str,
    org_override: str = "",
    private: bool = True,
    loop: bool = True,
    *,
    name_override: str = "",
    user_id: int | None = None,
    organization_id: int | None = None,
) -> tuple[Repo, Task]:
    """End-to-end: pick a name, create the GitHub repo, register it, queue a scaffold task.

    When ``name_override`` is provided, it's used as the repo slug (after
    sanitisation) instead of the LLM-generated one. Empty string falls back
    to Claude-picked names.
    """
    from shared.github_auth import get_github_token

    # repos.organization_id is NOT NULL (migration 027). Fail fast before
    # we hit GitHub, otherwise a missing org leaves an orphan repo on the
    # user's account when the DB insert later 500s.
    if organization_id is None:
        raise CreateRepoError(
            "organization_id is required — caller must be authenticated."
        )

    if not await get_github_token(user_id=user_id, organization_id=organization_id):
        raise CreateRepoError(
            "No GitHub auth configured — set GITHUB_TOKEN, or configure a "
            "GitHub App via GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY + "
            "GITHUB_APP_INSTALLATION_ID."
        )

    description = description.strip()
    if not description:
        raise CreateRepoError("Description is required.")

    # 1. Name: caller-supplied (sanitised) wins; otherwise ask Claude.
    if name_override.strip():
        name = _sanitize_slug(name_override)
        if not name or name == "new-project":
            raise CreateRepoError(
                f"'{name_override}' isn't a usable repo name — use letters, "
                f"digits, and hyphens."
            )
        log.info(f"Using caller-supplied repo name '{name}'")
    else:
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
    # ``mode='freeform'`` is critical for "Build something new": ``Repo.mode``
    # is what the standin resolver (``resolve_effective_mode``) reads to
    # decide whether the PO standin fires at every gate. Default DB value
    # is ``human_in_loop`` (conservative for legacy repos), so without this
    # the freeform standins never fire — children deadlock at the design
    # gate even though ``Task.freeform_mode=True``.
    # For "Build something new" runs the user-supplied description IS the
    # product brief — it's the only product-shaped context the PO standin
    # gets at every gate. Without it the standin falls back to deterministic
    # defaults (logged as ``plan_approval:no_product_brief``) and approves
    # designs without any grounding in what we're actually building. Stamp
    # the brief on both repo rows here so the freeform standins running
    # later read the real product context.
    full_repo = Repo(
        name=full_name, url=clone_url, default_branch=default_branch,
        organization_id=organization_id,
        mode="freeform",
        product_brief=description,
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
            mode="freeform",
            product_brief=description,
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

    # 6. Create the scaffold task.
    #
    # Title selection: the title is read by the intent-grill agent + PO
    # standin alongside the description. If we naively grab the first line
    # of the description and that line is a markdown section header
    # (e.g. ``## 1. What this service is, in one paragraph``), the agent
    # treats the task as "section 1 of a series of scaffolds" and writes a
    # foundation-only intent.md — leading to a trivial build (the user hit
    # this on the first harpoon attempt). Prefer the caller-supplied repo
    # ``name`` (which is what the user typed in the UI). Fall back to the
    # first non-structural line of the description.
    base_title = (name or _first_prose_line(description) or "new project").strip()
    title = base_title[:120]
    if not title.lower().startswith(("scaffold", "build", "create")):
        title = f"Scaffold: {title}"

    # ADR-018 — SCAFFOLD complexity routes to ``run_scaffold_parent`` (see
    # run.py::on_task_classified), which kicks off the intent-grill phase
    # before architects/builders run. The intent-grill phase produces the
    # refined intent.md the architects read, so we store the user's raw
    # description here verbatim instead of the long templated scaffold
    # instructions the old single-trio flow needed.
    #
    # Status stays at INTAKE so the standard ``on_task_created`` pipeline
    # picks the task up; the SCAFFOLD branch in ``on_task_classified``
    # owns the transition to AWAITING_INTENT_GRILL.
    task = Task(
        title=title,
        description=description,
        source=TaskSource.MANUAL,
        status=TaskStatus.INTAKE,
        complexity=TaskComplexity.SCAFFOLD,
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
