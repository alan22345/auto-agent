"""Deploy preview phase — push the task's branch to a dev environment.

Two paths:
  - GitHub Actions ``workflow_dispatch`` if a deploy-dev workflow exists in
    the PR's repo (preferred — runs in CI under proper credentials).
  - Local script fallback if a known deploy script is present in the
    workspace (used by self-hosted setups without GitHub Actions).
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from agent import sh
from agent.lifecycle._naming import _branch_name
from agent.lifecycle._orchestrator_api import get_task
from agent.workspace import WORKSPACES_DIR
from shared.events import (
    Event,
    publish,
    task_dev_deploy_failed,
    task_dev_deployed,
)
from shared.logging import setup_logging
from shared.types import TaskData

log = setup_logging("agent.lifecycle.deploy")


DEPLOY_WORKFLOW_NAMES = ["deploy-dev.yml"]
DEPLOY_SCRIPT_CANDIDATES = [
    "scripts/deploy-dev.sh",
    "scripts/deploy-dev",
    "scripts/deploy_dev.sh",
    "scripts/deploy.sh",
    "deploy-dev.sh",
    "deploy.sh",
]


async def handle_deploy_preview(task_id: int) -> None:
    """Deploy the task's branch to a dev environment."""
    task = await get_task(task_id)
    if not task or not task.repo_name:
        return

    branch_name = task.branch_name or await _branch_name(task_id, task.title)

    from shared.github_auth import get_github_token

    if task.pr_url and await get_github_token(
        user_id=task.created_by_user_id, organization_id=task.organization_id,
    ):
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

    from shared.github_auth import get_github_token

    token = await get_github_token(
        user_id=task.created_by_user_id, organization_id=task.organization_id,
    )
    headers = {
        "Authorization": f"token {token}",
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
        log.info(
            f"Task #{task_id}: triggering workflow '{deploy_workflow['name']}' "
            f"on branch '{branch_name}'"
        )

        resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            headers=headers,
            json={"ref": branch_name, "inputs": {"environment": "dev"}},
        )

        if resp.status_code == 204:
            conclusion = await _wait_for_workflow_run(
                owner, repo, workflow_id, branch_name, headers, task_id
            )
            factory = task_dev_deployed if conclusion == "success" else task_dev_deploy_failed
            await publish(
                factory(
                    task_id,
                    branch=branch_name,
                    output=f"Deploy workflow finished: {conclusion}",
                    pr_url=task.pr_url or "",
                )
            )
            return True
        else:
            log.warning(f"Task #{task_id}: workflow dispatch failed: {resp.status_code}")
            return False


async def _wait_for_workflow_run(
    owner: str,
    repo: str,
    workflow_id: int,
    branch: str,
    headers: dict,
    task_id: int,
    poll_interval: int = 30,
    max_wait: int = 1200,
) -> str:
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
        env = {"BRANCH": branch_name, "TASK_ID": str(task_id)}
        if deploy_script:
            script_path = os.path.join(workspace, deploy_script)
            os.chmod(script_path, 0o755)
            argv = [f"./{deploy_script}", branch_name]
        else:
            argv = ["make", "deploy-dev"]

        result = await sh.run(argv, cwd=workspace, timeout=300, env=env)

        if result.timed_out:
            await publish(
                task_dev_deploy_failed(
                    task_id,
                    branch=branch_name,
                    output="Deploy timed out",
                    pr_url=task.pr_url or "",
                )
            )
            return

        output = (result.stdout + result.stderr).strip()
        factory = task_dev_deployed if result.returncode == 0 else task_dev_deploy_failed
        await publish(
            factory(
                task_id,
                branch=branch_name,
                output=output[-1000:],
                pr_url=task.pr_url or "",
            )
        )

    except Exception:
        log.exception(f"Task #{task_id}: deploy preview failed")
        try:
            await publish(
                task_dev_deploy_failed(
                    task_id,
                    branch=branch_name,
                    output="Unexpected error",
                    pr_url=task.pr_url or "",
                )
            )
        except Exception:
            pass


async def handle(event: Event) -> None:
    """EventBus entry — deploys the task's branch to dev."""
    if not event.task_id:
        return
    await handle_deploy_preview(event.task_id)
