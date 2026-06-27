"""Differential (before/after) regression guard for health fixes.

A health fix is supposed to be behavior-preserving, so we boot the base
workspace (cleanup tip, pre-fix) and the branch workspace (post-fix),
exercise the same routes on each, and diff the responses. *Any* observable
divergence — a changed status, a changed body, or a changed boot state —
is treated as a regression and rejects the fix.

Body comparison is JSON-aware: responses that parse as JSON are compared
by structure+value (so key ordering and whitespace don't false-positive),
falling back to stripped-text equality otherwise. Nondeterministic fields
(timestamps, ids) are a known false-positive source handled by per-route
ignore lists in a later phase — for now any value change counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent.lifecycle.verify_primitives import (
    RouteResult,
    ServerHandle,
    boot_dev_server,
    exercise_routes,
)


@dataclass
class RouteDiff:
    """One observable divergence on a single route."""

    route: str
    detail: str


@dataclass
class DifferentialResult:
    """Outcome of a differential run. ``regressed`` is the gate.

    Result channels: route-level regressions populate ``diffs``; boot-level
    regressions (boot state diverged, or neither side booted) populate
    ``note``.
    """

    regressed: bool
    diffs: list[RouteDiff] = field(default_factory=list)
    note: str = ""


def _normalize_body(body: str):
    """Parse JSON if possible (order-insensitive), else stripped text.

    Known minor comparison quirks (accepted for now): ``json.loads`` coerces
    JSON numerics so ``123`` and ``123.0`` compare equal, and it accepts the
    non-standard ``NaN``/``Infinity`` tokens.
    """
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip()


def compare_route(route: str, base: RouteResult, branch: RouteResult) -> RouteDiff | None:
    """Return a :class:`RouteDiff` if the two responses diverge, else None.

    Status is compared exactly, then the ``ok`` verdict (so a flip from a
    passing verdict to a stub-shape/expected-shape-mismatch verdict at the
    same HTTP status+body is still caught), then the body JSON-aware (see
    :func:`_normalize_body`).
    """
    if base.status != branch.status:
        return RouteDiff(
            route=route,
            detail=f"status changed: {base.status} → {branch.status}",
        )
    if base.ok != branch.ok:
        return RouteDiff(
            route=route,
            detail=f"verdict changed: ok {base.ok} → {branch.ok} ({branch.reason or 'n/a'})",
        )
    if _normalize_body(base.body) != _normalize_body(branch.body):
        return RouteDiff(
            route=route,
            detail=f"body changed for {route}",
        )
    return None


def _diff_results(
    base: dict[str, RouteResult],
    branch: dict[str, RouteResult],
) -> list[RouteDiff]:
    """Diff two route→result maps over the union of their routes.

    A route present on one side but not the other is itself a divergence
    (the change added or removed an observable surface). Order is the
    sorted route order for determinism.
    """
    diffs: list[RouteDiff] = []
    for route in sorted(set(base) | set(branch)):
        b = base.get(route)
        r = branch.get(route)
        if b is None or r is None:
            present = "base" if b is not None else "branch"
            diffs.append(
                RouteDiff(
                    route=route,
                    detail=f"route missing on the other side (only in {present})",
                )
            )
            continue
        d = compare_route(route, b, r)
        if d is not None:
            diffs.append(d)
    return diffs


async def _boot_and_exercise(
    workspace: str, routes: list[str], repo_id: int | None
) -> tuple[ServerHandle, dict[str, RouteResult]]:
    """Boot one workspace and exercise ``routes`` against it.

    Returns the handle (caller owns teardown) and the route results. If the
    server is not ``running`` the results are empty.
    """
    handle = await boot_dev_server(workspace=workspace, repo_id=repo_id)
    if handle.state != "running":
        return handle, {}
    results = await exercise_routes(routes, handle=handle)
    return handle, results


async def differential_verify(
    *,
    base_workspace: str,
    branch_workspace: str,
    routes: list[str],
    repo_id: int | None = None,
) -> DifferentialResult:
    """Boot base + branch, exercise ``routes`` on each, and diff.

    ``regressed`` is True when any route response diverges OR the boot
    state diverges (one side boots, the other doesn't). When neither side
    boots there is no observable surface to compare — a degenerate
    no-regression with an explanatory note. Both servers are always torn
    down.
    """
    base_handle, base_results = await _boot_and_exercise(base_workspace, routes, repo_id)
    try:
        branch_handle, branch_results = await _boot_and_exercise(branch_workspace, routes, repo_id)
        try:
            base_up = base_handle.state == "running"
            branch_up = branch_handle.state == "running"

            if base_up != branch_up:
                return DifferentialResult(
                    regressed=True,
                    note=(
                        f"boot state diverged: base="
                        f"{base_handle.state}, branch={branch_handle.state}"
                    ),
                )
            if not base_up and not branch_up:
                return DifferentialResult(
                    regressed=False,
                    note="neither workspace booted a dev server; no routes compared",
                )

            diffs = _diff_results(base_results, branch_results)
            return DifferentialResult(regressed=bool(diffs), diffs=diffs)
        finally:
            await branch_handle.teardown()
    finally:
        await base_handle.teardown()
