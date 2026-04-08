"""Extract CI check commands from GitHub Actions workflow files.

Parses workflow YAML to find the commands that CI runs (lint, build, test, etc.)
so the agent can run them locally before pushing a PR.
"""

from __future__ import annotations

import logging
import base64
from typing import Any

import httpx

from shared.config import settings

log = logging.getLogger(__name__)


async def extract_ci_checks(repo_url: str) -> str | None:
    """Fetch workflow files from a GitHub repo and extract CI check commands.

    Returns a formatted string of commands to run, or None if no workflows found.
    """
    if not settings.github_token:
        return None

    # Parse owner/repo from URL
    # e.g. https://github.com/ergodic-ai/cardamon.git
    owner_repo = _parse_owner_repo(repo_url)
    if not owner_repo:
        return None

    owner, repo = owner_repo
    headers = {
        "Authorization": f"token {settings.github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # List workflow files
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/.github/workflows",
                headers=headers,
            )
            if resp.status_code != 200:
                log.info(f"No workflows found for {owner}/{repo}")
                return None

            workflow_files = resp.json()
            if not isinstance(workflow_files, list):
                return None

            all_checks: list[str] = []

            for wf_file in workflow_files:
                filename = wf_file.get("name", "")
                if not filename.endswith((".yml", ".yaml")):
                    continue

                # Skip deploy-only workflows
                lower_name = filename.lower()
                if any(skip in lower_name for skip in ["deploy", "release", "publish"]):
                    continue

                # Fetch workflow content
                resp = await client.get(
                    wf_file["url"],
                    headers=headers,
                )
                if resp.status_code != 200:
                    continue

                content = base64.b64decode(resp.json().get("content", "")).decode()
                checks = _parse_workflow_checks(content, filename)
                if checks:
                    all_checks.extend(checks)

            # Also parse deploy workflows for their test gate
            for wf_file in workflow_files:
                filename = wf_file.get("name", "")
                lower_name = filename.lower()
                if "deploy" in lower_name and filename.endswith((".yml", ".yaml")):
                    resp = await client.get(wf_file["url"], headers=headers)
                    if resp.status_code == 200:
                        content = base64.b64decode(resp.json().get("content", "")).decode()
                        checks = _parse_workflow_checks(content, filename, test_jobs_only=True)
                        if checks:
                            all_checks.extend(checks)

            if not all_checks:
                return None

            # Deduplicate while preserving order
            seen = set()
            unique_checks = []
            for check in all_checks:
                if check not in seen:
                    seen.add(check)
                    unique_checks.append(check)

            return "\n".join(unique_checks)

    except Exception:
        log.exception(f"Failed to extract CI checks for {owner}/{repo}")
        return None


def _parse_owner_repo(url: str) -> tuple[str, str] | None:
    """Extract owner/repo from a GitHub URL."""
    url = url.rstrip("/").removesuffix(".git")
    if "github.com" not in url:
        return None
    parts = url.split("/")
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def _parse_workflow_checks(content: str, filename: str, test_jobs_only: bool = False) -> list[str]:
    """Parse a workflow YAML and extract run commands that look like checks.

    Uses simple line parsing to avoid requiring PyYAML.
    """
    checks: list[str] = []
    in_run_block = False
    in_test_job = False
    current_job = ""

    # Keywords that indicate a check/validation step
    check_keywords = {
        "lint", "test", "check", "typecheck", "type-check", "tsc",
        "eslint", "prettier", "format", "validate", "build",
        "pytest", "jest", "vitest", "mocha", "cypress",
        "npm run", "npx ", "yarn ", "pnpm ",
        "cargo test", "cargo clippy", "cargo fmt",
        "go test", "go vet", "golint",
        "ruff", "flake8", "mypy", "black",
        "prisma generate", "prisma migrate",
    }

    # Keywords that indicate NOT a check (setup/deploy steps)
    skip_keywords = {
        "docker", "deploy", "push", "publish", "upload",
        "aws ", "az ", "gcloud", "kubectl",
        "npm install", "npm ci", "pip install", "apt-get",
        "actions/checkout", "actions/setup", "uses:",
        "echo ", "sleep ", "curl -I", "mkdir",
        "if [", "if [", "fi", "then", "else", "done",
        "find ", "seed", ">>", "basename",
    }

    lines = content.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()

        # Track which job we're in
        if not line.startswith(" ") and not line.startswith("\t") and stripped.endswith(":"):
            current_job = stripped.rstrip(":")

        # Detect job names (indented under 'jobs:')
        if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
            job_name = stripped.rstrip(":").strip()
            in_test_job = any(
                kw in job_name.lower() for kw in ["test", "lint", "check", "validate", "ci", "quality"]
            )

        if test_jobs_only and not in_test_job:
            continue

        # Look for 'run:' lines
        if "run:" in stripped and not stripped.startswith("#"):
            # Single-line run
            run_cmd = stripped.split("run:", 1)[1].strip()
            if run_cmd and run_cmd != "|":
                if _is_check_command(run_cmd, check_keywords, skip_keywords):
                    checks.append(run_cmd)
            elif run_cmd == "|":
                in_run_block = True
            continue

        # Multi-line run block
        if in_run_block:
            if stripped and not stripped.startswith("#") and not stripped.startswith("-"):
                # Check indentation — if we're back to step level, block is over
                indent = len(line) - len(line.lstrip())
                if indent <= 8 and not line.startswith("          "):
                    in_run_block = False
                    continue
                # Extract individual commands from the block
                for cmd in stripped.split("&&"):
                    cmd = cmd.strip()
                    if cmd and _is_check_command(cmd, check_keywords, skip_keywords):
                        checks.append(cmd)
            elif not stripped:
                continue
            else:
                in_run_block = False

    return checks


def _is_check_command(cmd: str, check_keywords: set[str], skip_keywords: set[str]) -> bool:
    """Determine if a command looks like a CI check (lint/test/build)."""
    cmd_lower = cmd.lower()

    # Skip setup/deploy commands
    if any(skip in cmd_lower for skip in skip_keywords):
        return False

    # Match check commands
    return any(kw in cmd_lower for kw in check_keywords)
