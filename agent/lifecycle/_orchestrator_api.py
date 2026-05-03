"""HTTP calls into the orchestrator API.

Every lifecycle module that needs to fetch task/repo state or transition a
task uses these helpers. The orchestrator URL comes from ``shared.config``.
"""

from __future__ import annotations

import httpx

from shared.config import settings
from shared.events import Event, publish
from shared.types import FreeformConfigData, RepoData, TaskData

ORCHESTRATOR_URL = settings.orchestrator_url


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


async def get_freeform_config(repo_name: str) -> FreeformConfigData | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/freeform/config")
        if resp.status_code != 200:
            return None
        configs = resp.json()
        for cfg in configs:
            cfg_data = FreeformConfigData.model_validate(cfg)
            if cfg_data.repo_name == repo_name and cfg_data.enabled:
                return cfg_data
    return None


async def transition_task(task_id: int, status: str, message: str = "") -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/transition",
            json={"status": status, "message": message},
        )
    if status in ("failed", "blocked", "done"):
        await publish(
            Event(
                type=f"task.{status}",
                task_id=task_id,
                payload={"error": message} if status in ("failed", "blocked") else {},
            )
        )
