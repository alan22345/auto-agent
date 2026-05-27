"""Phase E — project-level final verification — ADR-018 §8.

After every child trio has reached a terminal state, the scaffold
parent boots the integrated whole and asks: does it actually run, and
does it address the original intent? The verifier is functional, not a
code reviewer — stub-scanning lives in the per-domain trio reviewers.

Layers:

- ``boot_dev_server`` — boots the project.
- ``exercise_routes`` — hits the union of every child item's
  ``affected_routes`` (sourced from each child's ``.auto-agent/backlog.json``).
- ``inspect_ui`` — Playwright screenshot + a vision-LLM call per UI
  route, with ``.auto-agent/intent.md`` (the original scaffold spec)
  passed as the intent so the judge can compare the rendered screen to
  what was promised.

We synthesise a verdict from those signals, write
``.auto-agent/scaffold_final_verification.json``, and return ``"passed"``
or ``"gaps_found"`` so the parent driver can transition appropriately.
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
from sqlalchemy import select

from agent.lifecycle import verify_primitives
from agent.lifecycle.route_inference import is_ui_route
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.workspace_paths import (
    BACKLOG_PATH,
    INTENT_PATH,
    SCAFFOLD_FINAL_VERIFICATION_PATH,
)
from agent.workspace import _workspace_path
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


def _load_child_routes(child_workspace: str) -> list[str]:
    """Read a child workspace's backlog.json and return all item routes.

    Returns ``[]`` for any I/O / parse failure — child workspaces may
    have been cleaned up by the time the scaffold parent runs final
    verification; a missing backlog should not block the verdict.
    """

    backlog_path = os.path.join(child_workspace, BACKLOG_PATH)
    try:
        with open(backlog_path) as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "scaffold.final_verification.backlog_unreadable",
            backlog_path=backlog_path,
            error=str(exc),
        )
        return []
    if isinstance(payload, dict):
        items = payload.get("items")
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    if not isinstance(items, list):
        return []
    routes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for r in item.get("affected_routes") or []:
            if isinstance(r, str) and r:
                routes.append(r)
    return routes


async def _collect_union_routes(parent_id: int) -> list[str]:
    """Union of ``affected_routes`` across every child's backlog items.

    Reads each child's ``<workspace>/.auto-agent/backlog.json`` directly —
    ``Task.affected_routes`` is not populated by the trio (and we'd
    rather not introduce a denormalised mirror).
    """

    async with async_session() as s:
        children = (
            (await s.execute(select(Task).where(Task.parent_task_id == parent_id))).scalars().all()
        )

    seen: set[str] = set()
    routes: list[str] = []
    for child in children:
        child_workspace = _workspace_path(task_id=child.id, organization_id=child.organization_id)
        for r in _load_child_routes(child_workspace):
            if r not in seen:
                seen.add(r)
                routes.append(r)
    return routes


def _read_intent(workspace: str) -> str:
    """Return the scaffold's ``intent.md`` body, or '' if unreadable.

    ``intent.md`` is the only artefact representing the original design;
    we hand it to ``inspect_ui`` so the vision judge can compare each
    rendered route against what was promised.
    """

    path = os.path.join(workspace, INTENT_PATH)
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


async def run(task: Task) -> str:
    """Run project-level verification. Returns ``"passed"`` or ``"gaps_found"``.

    Side effect: writes ``.auto-agent/scaffold_final_verification.json``.
    """

    workspace = await prepare_scaffold_workspace(task)
    routes = await _collect_union_routes(task.id)
    intent = _read_intent(workspace)

    gaps: list[dict[str, Any]] = []
    summary_lines: list[str] = []

    handle = await verify_primitives.boot_dev_server(workspace=workspace, repo_id=task.repo_id)
    try:
        if handle.state == "failed":
            gaps.append(
                {
                    "kind": "boot_failure",
                    "description": (f"Dev server failed to boot: {handle.failure_reason}"),
                }
            )
            summary_lines.append(f"Boot failed: {handle.failure_reason}")
        elif handle.state == "running":
            if not routes:
                summary_lines.append("Boot ok; no routes declared by children.")
            else:
                route_results = await verify_primitives.exercise_routes(routes, handle=handle)
                ok_count = sum(1 for r in route_results.values() if r.ok)
                summary_lines.append(f"Exercised {len(route_results)} route(s); {ok_count} ok.")
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

                ui_routes = [r for r, rr in route_results.items() if rr.ok and is_ui_route(r)]
                ui_ok = 0
                ui_failed = 0
                for route in ui_routes:
                    ui = await verify_primitives.inspect_ui(
                        route=route, intent=intent, base_url=handle.base_url
                    )
                    if ui.ok:
                        ui_ok += 1
                        continue
                    if "playwright_not_installed" in (ui.reason or ""):
                        continue
                    ui_failed += 1
                    gaps.append(
                        {
                            "kind": "ui_mismatch",
                            "route": route,
                            "description": (f"UI for {route} does not match intent: {ui.reason}"),
                        }
                    )
                if ui_routes:
                    summary_lines.append(
                        f"Inspected {len(ui_routes)} UI route(s); "
                        f"{ui_ok} match intent, {ui_failed} flagged."
                    )
        else:
            summary_lines.append(
                f"Smoke skipped (boot state={handle.state}, routes={len(routes)})."
            )
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
        route_count=len(routes),
    )
    return verdict


__all__ = ["run"]
