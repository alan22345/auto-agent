"""PR-reviewer agent role — ADR-015 §5 Phase 4 (correctness scope only).

The simple flow has no plan-approval and no final-review gate, so the
PR reviewer is the **only** full verify gate the flow has. Per ADR-015
§5 it runs the shared :mod:`agent.lifecycle.verify_primitives` against
the open PR end-to-end:

  1. ``grep_diff_for_stubs(diff)`` — block on no-defer violations.
  2. ``boot_dev_server`` + ``exercise_routes(routes_inferred_from_diff)``.
  3. ``inspect_ui`` for any UI route touched.

The verdict + comments are written to ``.auto-agent/pr_review.json`` so
the orchestrator (and any human teammate) can pick them up with the same
:mod:`agent.lifecycle.workspace_reader` primitives as every other gate
file in the skills bridge.

Two scopes are defined in the ADR; only ``correctness`` lands in this
phase:

- ``correctness`` (simple flow) — what this module implements.
- ``artefact``   (complex / complex_large) — deferred to Phase 7. Raises
  :class:`ScopeNotYetImplemented` so a mis-routed call fails loudly
  rather than producing a misleading verdict.

Note on the skill seam: the matching ``submit-pr-review`` skill in
``skills/auto-agent/`` is wired so a future iteration can drive the PR
review through an *agent* invocation (LLM authors verdict + comments,
writes ``pr_review.json`` via the skill). For Phase 4 the reviewer is a
pure-Python pipeline of shared primitives — no LLM in the loop. We
still write the same ``pr_review.json`` shape, so the file-format
contract is stable when the LLM seam later replaces the body.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from agent import sh
from agent.lifecycle.verify_primitives import (
    RouteResult,
    ServerHandle,
    StubResult,
    UIResult,
    boot_dev_server,
    exercise_routes,
    grep_diff_for_stubs,
    inspect_ui,
)
from agent.lifecycle.workspace_paths import AUTO_AGENT_DIR, PR_REVIEW_PATH
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.pr_reviewer")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class PRReviewResult:
    """Outcome of a single PR-review pass.

    Mirrors the on-disk ``pr_review.json`` shape so writes / reads round-trip
    losslessly (see :func:`_write_pr_review_json`).
    """

    verdict: Literal["approved", "changes_requested"]
    comments: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


class ScopeNotYetImplemented(NotImplementedError):  # noqa: N818  # established pattern: QuotaExceeded, BootTimeout
    """Raised when a caller invokes a PR-review scope this phase hasn't
    built yet (currently: ``"artefact"``).

    Subclass of :class:`NotImplementedError` so callers that have a broader
    catch keep working, but the dedicated class lets tests pin the exact
    deferral point without false positives on other ``NotImplementedError``
    paths in the codebase. Phase 7 lands the artefact-scope prompt +
    commit-narrative review and removes this raise site.
    """


# ---------------------------------------------------------------------------
# Route inference — simple regex over FastAPI decorators + Next.js pages.
# ---------------------------------------------------------------------------


_FASTAPI_DECORATOR_RE = re.compile(
    r'@\w+\.(get|post|put|patch|delete)\(\s*"([^"]+)"',
    re.IGNORECASE,
)
_FASTAPI_DECORATOR_SINGLE_QUOTE_RE = re.compile(
    r"@\w+\.(get|post|put|patch|delete)\(\s*'([^']+)'",
    re.IGNORECASE,
)


def _file_path_from_diff_header(line: str) -> str | None:
    """Return the post-image filename from a '+++ b/<path>' diff header."""

    if not line.startswith("+++ "):
        return None
    rest = line[4:].strip()
    if rest.startswith("b/"):
        return rest[2:]
    if rest == "/dev/null":
        return None
    return rest


def _route_from_nextjs_page_path(path: str) -> str | None:
    """Map a Next.js App Router page path to its URL route.

    Examples:
      - ``web-next/app/(app)/dashboard/page.tsx`` → ``/dashboard``
      - ``web-next/app/repos/[id]/page.tsx`` → ``/repos/[id]``
      - ``web-next/app/page.tsx`` → ``/``

    Returns ``None`` for non-page files. The mapping is intentionally
    conservative: route-groups in parentheses (``(app)``) are stripped per
    Next.js convention, but ``[param]`` segments are kept as-is — the
    smoke-test harness can substitute them.
    """

    if not path.startswith("web-next/app/") or not path.endswith("/page.tsx"):
        return None
    inner = path[len("web-next/app/") : -len("/page.tsx")]
    parts = [p for p in inner.split("/") if not (p.startswith("(") and p.endswith(")"))]
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def infer_routes_from_diff(diff: str) -> list[str]:
    """Return the de-duplicated list of routes touched by ``diff``.

    Heuristic:
      - any added line matching ``@<router>.get|post|put|patch|delete("...")``
        contributes its path.
      - any file added/modified under ``web-next/app/...page.tsx`` contributes
        the URL implied by its directory.

    Order-preserving so tests can assert on the first route.
    """

    routes: list[str] = []
    seen: set[str] = set()

    current_file: str | None = None
    in_hunk = False

    def _add(route: str) -> None:
        if route and route not in seen:
            routes.append(route)
            seen.add(route)

    for raw in (diff or "").splitlines():
        header_path = _file_path_from_diff_header(raw)
        if raw.startswith("+++ "):
            current_file = header_path
            in_hunk = False
            if current_file:
                page_route = _route_from_nextjs_page_path(current_file)
                if page_route:
                    _add(page_route)
            continue
        if raw.startswith("--- "):
            in_hunk = False
            continue
        if raw.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            line = raw[1:]
            for matcher in (_FASTAPI_DECORATOR_RE, _FASTAPI_DECORATOR_SINGLE_QUOTE_RE):
                m = matcher.search(line)
                if m:
                    _add(m.group(2))

    return routes


# ---------------------------------------------------------------------------
# UI-route heuristic — anything that looks like a frontend page route.
# ---------------------------------------------------------------------------


def _is_ui_route(route: str) -> bool:
    """A route is treated as UI if it does not start with an API prefix.

    Conservative: ``/api/...`` and ``/v1/...`` are obviously not UI. Anything
    else (``/dashboard``, ``/`` , ``/widgets``) gets passed to
    :func:`inspect_ui`. ``inspect_ui`` is itself graceful — it returns
    ``playwright_not_installed`` when the browser binding is unavailable,
    which the PR reviewer treats as a non-blocking skip (UI inspection is
    advisory in this phase; the route exercise + stub-grep are the hard
    gates).
    """

    if not route.startswith("/"):
        return False
    api_prefixes = ("/api/", "/v1/")
    return not any(route.startswith(p) for p in api_prefixes)


# ---------------------------------------------------------------------------
# PR diff loader — uses the shared subprocess seam (ADR-010).
# ---------------------------------------------------------------------------


async def _load_pr_diff(workspace_root: str, *, base_branch: str = "main") -> str:
    """Return the unified diff of HEAD vs ``base_branch``.

    Falls back to a single-commit diff (``HEAD~1..HEAD``) if the base ref is
    unknown locally — that happens when the workspace was cloned shallow
    against the branch and ``main`` was never fetched. The fallback is
    enough for the in-PR commit chain to be inspected.
    """

    result = await sh.run(
        ["git", "diff", f"{base_branch}...HEAD"],
        cwd=workspace_root,
        timeout=30,
    )
    if not result.failed and result.stdout.strip():
        return result.stdout
    fallback = await sh.run(
        ["git", "diff", "HEAD~1..HEAD"],
        cwd=workspace_root,
        timeout=30,
    )
    return fallback.stdout if not fallback.failed else ""


# ---------------------------------------------------------------------------
# pr_review.json — single source of truth for the schema we emit on disk.
# ---------------------------------------------------------------------------


def _write_pr_review_json(workspace_root: str, result: PRReviewResult) -> None:
    """Persist the verdict so the orchestrator can read it.

    We write the file directly from Python because the agent-invocation seam
    that would route this through the ``submit-pr-review`` skill isn't fully
    wired in this phase (the simple-flow PR reviewer runs as a pure-Python
    pipeline, not an agent prompt). The on-disk format matches the skill's
    JSON shape exactly so a future Phase-7 swap to an LLM-author path is
    file-format-compatible.
    """

    target = os.path.join(workspace_root, PR_REVIEW_PATH)
    os.makedirs(os.path.join(workspace_root, AUTO_AGENT_DIR), exist_ok=True)
    payload = {
        "schema_version": "1",
        "verdict": result.verdict,
        "comments": result.comments,
        "summary": result.summary,
    }
    with open(target, "w") as fh:
        json.dump(payload, fh, indent=2)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


async def run_pr_review(
    *,
    task: Any,
    workspace_root: str,
    scope: Literal["correctness", "artefact"],
) -> PRReviewResult:
    """Run the self-PR-review for ``task`` against the workspace's PR diff.

    ``scope`` selects the prompt / pipeline:

    - ``"correctness"`` — pure-Python pipeline running the shared verify
      primitives end-to-end. Used by the simple flow as its only verify
      gate. ✅ Implemented in this phase.
    - ``"artefact"`` — LLM-authored review of PR hygiene, commit narrative,
      and description coherence (no re-run of smoke; the final-review or
      verify gate already covered correctness). 🛑 Phase 7. Raises
      :class:`ScopeNotYetImplemented`.

    The function always writes ``.auto-agent/pr_review.json`` on a normal
    return so the orchestrator's gate-file reader finds something.
    """

    if scope == "artefact":
        # Phase-7 placeholder: artefact-scope prompt + commit-narrative review.  # auto-agent: allow-stub
        # Raising here is deliberate — a clearly-passing PRReviewResult would
        # let a mis-routed call silently green-light a PR. The opt-out marker
        # on the comment above is intentional: the no-defer grep would
        # otherwise flag the phase-number reference; the typed exception is
        # the actual safety net (callers know to catch it before Phase 7).
        raise ScopeNotYetImplemented(
            "artefact-scope PR review is not yet implemented (ADR-015 §5; lands in the next sub-phase)"
        )

    if scope != "correctness":
        raise ValueError(f"unknown PR review scope: {scope!r}")

    return await _run_correctness_review(task=task, workspace_root=workspace_root)


# ---------------------------------------------------------------------------
# Correctness scope implementation.
# ---------------------------------------------------------------------------


async def _run_correctness_review(
    *,
    task: Any,
    workspace_root: str,
) -> PRReviewResult:
    """Run the four verify primitives against the PR diff and synthesise a verdict."""

    base_branch = getattr(task, "base_branch", None) or "main"

    diff = await _load_pr_diff(workspace_root, base_branch=base_branch)

    comments: list[dict[str, Any]] = []

    # ---------------------------------------------------------------------
    # Layer 1 — diff-grep for no-defer violations.
    # ---------------------------------------------------------------------
    stub_result: StubResult = grep_diff_for_stubs(diff)
    blocking_stubs = [v for v in stub_result.violations if not v.allowed_via_optout]
    for v in blocking_stubs:
        comments.append(
            {
                "path": v.file,
                "line": v.line,
                "comment": (
                    f"No-defer violation: '{v.pattern}' found — {v.snippet.strip()}. "
                    f"This is layer 4 of the no-defer gate (ADR-015 §8); add "
                    f"'# auto-agent: allow-stub' on the line if intentional."
                ),
            }
        )

    # ---------------------------------------------------------------------
    # Layer 2 — route exercise. Only boot the server when we found routes.
    # ---------------------------------------------------------------------
    routes = infer_routes_from_diff(diff)
    route_results: dict[str, RouteResult] = {}

    handle: ServerHandle | None = None
    try:
        if routes:
            handle = await boot_dev_server(workspace=workspace_root)
            if handle.state == "running":
                route_results = await exercise_routes(routes, handle=handle)
            elif handle.state == "failed":
                comments.append(
                    {
                        "comment": (
                            f"dev server boot failed: {handle.failure_reason or 'unknown'}; "
                            f"cannot exercise affected routes {routes!r}."
                        ),
                    }
                )
            # "disabled" → no smoke config; treat as advisory skip and proceed.

        # -----------------------------------------------------------------
        # Layer 3 — UI inspection on UI-flavoured routes that returned 2xx.
        # -----------------------------------------------------------------
        if handle and handle.state == "running":
            for route, rr in route_results.items():
                if not _is_ui_route(route) or not rr.ok:
                    continue
                ui: UIResult = await inspect_ui(
                    route=route,
                    intent=getattr(task, "description", "") or getattr(task, "title", ""),
                    base_url=handle.base_url,
                )
                if not ui.ok:
                    # UI failures from missing-playwright are advisory in this
                    # phase (callers can still ship without a headless browser).
                    if "playwright_not_installed" in ui.reason:
                        log.info(
                            "pr_review.ui_inspection_skipped",
                            route=route,
                            reason=ui.reason,
                        )
                        continue
                    comments.append(
                        {
                            "path": route,
                            "comment": f"UI inspection failed for {route}: {ui.reason}",
                        }
                    )

    finally:
        if handle is not None:
            await handle.teardown()

    # Synthesise route-level comments AFTER UI to keep ordering deterministic.
    for route, rr in route_results.items():
        if rr.ok:
            continue
        comments.append(
            {
                "path": route,
                "comment": (
                    f"Route {route} returned status={rr.status}, reason={rr.reason!r}. "
                    f"This is the correctness-scope verify gate; please fix the route "
                    f"so it returns 2xx before re-running the PR review."
                ),
            }
        )

    verdict: Literal["approved", "changes_requested"] = (
        "changes_requested" if comments else "approved"
    )

    summary_lines: list[str] = []
    summary_lines.append(
        f"correctness review: {len(blocking_stubs)} stub(s), "
        f"{sum(1 for r in route_results.values() if not r.ok)} failing route(s)."
    )
    if routes:
        summary_lines.append(f"routes inferred from diff: {routes!r}")
    else:
        summary_lines.append("no routes inferred from diff; route exercise skipped.")
    summary = " ".join(summary_lines)

    result = PRReviewResult(verdict=verdict, comments=comments, summary=summary)
    _write_pr_review_json(workspace_root, result)
    return result
