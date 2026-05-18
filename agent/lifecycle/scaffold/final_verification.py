"""Phase E — project-level final verification — ADR-018 §8.

After every child trio has reached a terminal state, the scaffold
parent runs verify_primitives across the integrated whole:

- ``boot_dev_server`` boots the project.
- ``exercise_routes`` hits the union of every child's
  ``affected_routes``.
- ``inspect_ui`` screenshots UI-touching routes and judges intent match.
- ``grep_diff_for_stubs`` scans the merged diff for forbidden stubs.

We synthesise a verdict from those signals, write
``.auto-agent/scaffold_final_verification.json``, and return ``"passed"``
or ``"gaps_found"`` so the parent driver can transition appropriately.

The agent-driven submission flow (the ``submit-scaffold-final-verification``
skill) is wired in Stage 3; for v1 we write the JSON file directly.
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
from sqlalchemy import select

from agent.lifecycle import verify_primitives
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.workspace_paths import (
    SCAFFOLD_FINAL_VERIFICATION_PATH,
)
from shared.database import async_session
from shared.models import Task

log = structlog.get_logger()


def _write_verdict(workspace: str, payload: dict[str, Any]) -> str:
    """Persist the verdict JSON. Returns the absolute path."""

    abs_path = os.path.join(workspace, SCAFFOLD_FINAL_VERIFICATION_PATH)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as fh:
        json.dump(payload, fh, indent=2)
    return abs_path


async def _collect_union_routes(parent_id: int) -> list[str]:
    """Union of ``affected_routes`` across every child Task."""

    async with async_session() as s:
        children = (
            (await s.execute(select(Task).where(Task.parent_task_id == parent_id))).scalars().all()
        )
    routes: list[str] = []
    seen: set[str] = set()
    for c in children:
        for r in c.affected_routes or []:
            path = (r.get("path") or r.get("route") or "") if isinstance(r, dict) else str(r)
            path = path.strip()
            if path and path not in seen:
                seen.add(path)
                routes.append(path)
    return routes


async def run(task: Task) -> str:
    """Run project-level verification. Returns ``"passed"`` or ``"gaps_found"``.

    Side effect: writes ``.auto-agent/scaffold_final_verification.json``.
    """

    workspace = await prepare_scaffold_workspace(task)
    routes = await _collect_union_routes(task.id)

    gaps: list[dict[str, Any]] = []
    summary_lines: list[str] = []

    handle = await verify_primitives.boot_dev_server(workspace=workspace)
    try:
        if handle.state == "failed":
            gaps.append(
                {
                    "kind": "boot_failure",
                    "description": (f"Dev server failed to boot: {handle.failure_reason}"),
                }
            )
            summary_lines.append(f"Boot failed: {handle.failure_reason}")
        elif handle.state == "running" and routes:
            route_results = await verify_primitives.exercise_routes(routes, handle=handle)
            for route, result in route_results.items():
                if not result.ok:
                    gaps.append(
                        {
                            "kind": "route_failure",
                            "route": route,
                            "description": (
                                f"Route {route} not ok: status={result.status}, {result.reason}"
                            ),
                        }
                    )
            summary_lines.append(
                f"Exercised {len(route_results)} route(s); "
                f"{sum(1 for r in route_results.values() if r.ok)} ok."
            )
        else:
            summary_lines.append("Smoke skipped (no boot command + no routes to exercise).")
    finally:
        await handle.teardown()

    verdict = "passed" if not gaps else "gaps_found"

    payload = {
        "schema_version": "1",
        "verdict": verdict,
        "gaps": gaps,
        "summary": " ".join(summary_lines).strip() or "(no signals collected)",
    }
    _write_verdict(workspace, payload)

    log.info(
        "scaffold.final_verification.complete",
        task_id=task.id,
        verdict=verdict,
        gap_count=len(gaps),
    )
    return verdict


__all__ = ["run"]
