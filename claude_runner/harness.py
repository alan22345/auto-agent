"""Harness engineering — onboard repos with structure, linting, and entropy management.

When auto-agent encounters a repo for the first time, it runs a harness onboarding
pass: analyzing the codebase, then raising a PR with tailored infrastructure that
channels the AI agent's output into reliable, maintainable code.

The three pillars:
1. Context engineering — ADR docs, CLAUDE.md with constraints, indexed knowledge
2. Architectural constraints — linting rules, dependency layer enforcement, pre-commit hooks
3. Entropy management — dead code detection, stale doc scanning, naming drift checks
"""

from __future__ import annotations

import asyncio
import os

import httpx

from shared.config import settings
from shared.logging import setup_logging
from shared.types import RepoData

from claude_runner.workspace import clone_repo, push_branch, create_branch

log = setup_logging("harness")

ORCHESTRATOR_URL = settings.orchestrator_url


HARNESS_ONBOARDING_PROMPT = """\
You are onboarding this repository for an autonomous AI coding agent (auto-agent).
Your job is to add "harness engineering" infrastructure that keeps AI-generated code
high quality, well-structured, and maintainable.

## What to do

Analyze this repo's tech stack, structure, and patterns, then add the following
infrastructure. Adapt everything to this specific repo — do NOT use generic templates.

### 1. CLAUDE.md (create or update)

Create a `CLAUDE.md` in the repo root with:
- **Build/test/lint commands**: exact commands to build, test, and lint this project
- **Architectural constraints**: define the dependency layers for THIS repo
  (e.g., for a Python project: `types/ -> config/ -> services/ -> api/`)
  (e.g., for a React app: `types/ -> hooks/ -> components/ -> pages/`)
- **Module boundaries**: which directories own which concerns, what should NOT import what
- **Code style rules**: patterns specific to this repo (naming, error handling, etc.)
- **File organization rules**: where new files should go, max file size guidelines

Keep it concise and actionable. This file is the agent's primary guide.

### 2. Linting & Static Analysis

Based on the tech stack, add or enhance linting configuration:

**Python repos:**
- `ruff.toml` or `pyproject.toml [tool.ruff]` — strict rules including import sorting,
  unused variable detection, and banned imports between layers
- If `import-linter` makes sense for the project size, add `.importlinter` config
  defining dependency layers

**JavaScript/TypeScript repos:**
- `.eslintrc` / `eslint.config.js` — strict rules including no-unused-vars,
  import/order, boundaries plugin if applicable
- `tsconfig.json` strictness (if TypeScript and not already strict)

**Go repos:**
- `golangci-lint` config (`.golangci.yml`) with strict rules

**Other stacks:** add the idiomatic linter config for that ecosystem.

Only add tools that are standard for the ecosystem. Do NOT add exotic dependencies.

### 3. Pre-commit hooks

Add or update `.pre-commit-config.yaml` with hooks for:
- Linting (the tools configured above)
- Trailing whitespace, end-of-file fixer
- Large file detection (prevent accidental binary commits)

If the repo already uses a different hook system (husky, lefthook), use that instead.

### 4. Architecture Decision Records (ADR)

Create `docs/decisions/` with:
- `000-template.md` — a simple ADR template (title, status, context, decision, consequences)
- `001-harness-engineering.md` — documenting THIS onboarding as the first ADR

### 5. Entropy management config

Create `.auto-agent/entropy.yml` with scanning rules tailored to this repo:
```yaml
# Auto-agent entropy management config
# These checks run during idle time to keep the codebase clean

checks:
  dead_code:
    enabled: true
    tool: "<appropriate tool for stack>"  # vulture for Python, ts-prune for TS, etc.
    exclude: ["migrations/", "scripts/"]

  unused_imports:
    enabled: true
    # Handled by linter above

  stale_docs:
    enabled: true
    doc_dirs: ["docs/", "README.md"]
    max_age_days: 90

  naming_drift:
    enabled: true
    # Flag files/functions that don't match repo conventions

  large_files:
    enabled: true
    max_size_kb: 500
    exclude: ["*.lock", "*.svg"]
```

Adapt the tools and excludes to what makes sense for THIS repo.

## Rules

- Do NOT change any existing application code or tests.
- Do NOT break existing CI/CD or workflows.
- If the repo already has good linting, enhance it — don't replace it.
- If CLAUDE.md already exists, preserve existing content and add the architectural
  constraints section.
- Keep all additions minimal and idiomatic for the tech stack.
- Commit with message: "Add harness engineering infrastructure for auto-agent"

## After implementation

1. Review your own changes — make sure nothing breaks existing workflows.
2. Commit all changes in a single commit.
"""


async def handle_harness_onboarding(repo_id: int, repo_name: str) -> str | None:
    """Run harness onboarding for a repo. Returns PR URL or None on failure."""
    log.info(f"Starting harness onboarding for repo '{repo_name}'")

    # Fetch repo data
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/repos")
        repos = resp.json()

    repo_data = None
    for r in repos:
        rd = RepoData.model_validate(r)
        if rd.id == repo_id:
            repo_data = rd
            break

    if not repo_data:
        log.error(f"Repo '{repo_name}' (id={repo_id}) not found")
        return None

    # Clone repo into a repo-specific workspace to avoid collisions between repos
    ws_name = f"harness-{repo_name.replace('/', '-')}"
    workspace = await clone_repo(repo_data.url, task_id=0, default_branch=repo_data.default_branch, workspace_name=ws_name)

    branch_name = "auto-agent/harness-onboarding"

    try:
        await create_branch(workspace, branch_name)

        # Run Claude Code with the harness onboarding prompt
        proc = await asyncio.create_subprocess_exec(
            "claude", "--print", "--dangerously-skip-permissions",
            HARNESS_ONBOARDING_PROMPT,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1200)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.error(f"Harness onboarding timed out for '{repo_name}'")
            return None

        output = (stdout or b"").decode() + (stderr or b"").decode()
        log.info(f"Harness onboarding output for '{repo_name}': {output[:500]}...")

        # Push the branch
        await push_branch(workspace, branch_name)

        # Create PR
        env = os.environ.copy()
        env["GH_TOKEN"] = settings.github_token

        pr_body = (
            "## Harness Engineering Onboarding\n\n"
            "This PR adds infrastructure to keep AI-generated code high quality:\n\n"
            "### What's included\n"
            "- **CLAUDE.md** — Architectural constraints and conventions for the AI agent\n"
            "- **Linting config** — Strict rules enforced mechanically (not by AI judgment)\n"
            "- **Pre-commit hooks** — Catch violations before they land\n"
            "- **ADR system** — `docs/decisions/` for indexed design decisions\n"
            "- **Entropy config** — `.auto-agent/entropy.yml` for dead code/stale doc scanning\n\n"
            "### Why\n"
            "AI agents are powerful but directionless. This infrastructure channels their output\n"
            "into reliable, maintainable code by enforcing structure mechanically.\n\n"
            "**Please review carefully** — these constraints will guide all future auto-agent work\n"
            "on this repo.\n\n"
            "---\n"
            "*Generated by auto-agent harness engineering*"
        )

        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create",
            "--title", f"[auto-agent] Harness engineering onboarding for {repo_name.split('/')[-1]}",
            "--body", pr_body,
            "--base", repo_data.default_branch,
            "--head", branch_name,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        pr_url = (stdout or b"").decode().strip()
        stderr_str = (stderr or b"").decode().strip()

        if proc.returncode != 0:
            log.error(f"PR creation failed for harness onboarding: {stderr_str or pr_url}")
            return None

        if not pr_url.startswith("http"):
            log.error(f"Invalid PR URL from gh: {pr_url!r}")
            return None

        log.info(f"Harness onboarding PR created for '{repo_name}': {pr_url}")

        # Update repo record
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/repos/{repo_id}/harness",
                json={"harness_onboarded": True, "harness_pr_url": pr_url},
            )

        return pr_url

    except Exception:
        log.exception(f"Harness onboarding failed for '{repo_name}'")
        return None
