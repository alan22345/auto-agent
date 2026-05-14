"""Trio reviewer — per-item heavy reviewer + legacy alignment-only fallback.

ADR-015 §3 / Phase 7 introduces the **heavy** per-item reviewer that
replaces the readonly alignment-only contract from ADR-013. For one
backlog item it runs alignment + grep + smoke + UI in one pass and
writes ``.auto-agent/reviews/<item_id>.json`` via the
``submit-item-review`` skill. The legacy ``handle_trio_review`` is kept
for the existing trio child-task code path that hasn't migrated yet
(coding.py still imports it for the trio-child verify hand-off); the
new ``run_heavy_review`` is the Phase 7+ public entry point.

Verdict path (legacy):
  * ``ok=true`` → child transitions to ``PR_CREATED`` and
    ``_open_pr_and_advance`` runs (push branch + open PR).
  * ``ok=false`` or invalid JSON → child transitions back to ``CODING``
    with the feedback as the retry reason.

Verdict path (heavy, Phase 7):
  * ``verdict="pass"`` → caller advances the dispatcher loop.
  * ``verdict="fail"`` with a synthesised reason → caller re-runs the
    builder with that reason as feedback (bounded; 3 cycles per item
    before architect tiebreak — see :mod:`agent.lifecycle.trio.dispatcher`).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select

from agent import sh
from agent.lifecycle._naming import _fresh_session_id
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.route_inference import infer_routes_from_diff, is_ui_route
from agent.lifecycle.trio.prompts import TRIO_REVIEWER_SYSTEM
from agent.lifecycle.verify_primitives import (
    ServerHandle,
    boot_dev_server,
    exercise_routes,
    grep_diff_for_stubs,
    inspect_ui,
)
from agent.lifecycle.workspace_paths import review_path
from shared.database import async_session
from shared.models import Task, TaskStatus, TrioReviewAttempt

log = structlog.get_logger()

_JSON_RE = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)


def _extract_verdict(text: str) -> dict | None:
    """Extract ``{"ok": bool, "feedback": str}`` from a fenced JSON block.

    Handles:
      - No JSON block → ``None``
      - Malformed JSON → skip that block, try the next
      - Missing ``ok`` key → skip that block
      - Multiple blocks → prefer the last valid one

    Returns ``None`` on total failure. ``feedback`` defaults to ``""`` when
    absent so callers can rely on it being present.
    """
    if not text:
        return None
    matches = list(_JSON_RE.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        try:
            v = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(v, dict) or "ok" not in v:
            continue
        v.setdefault("feedback", "")
        return v
    return None


def _result_output(result: Any) -> str:
    """Extract text output from whatever ``agent.run`` returned."""
    if hasattr(result, "output"):
        return result.output or ""
    return str(result) if result is not None else ""


def _result_tool_calls(result: Any, agent: Any) -> list:
    """Best-effort tool-call log — mirrors the architect helper."""
    for src in (agent, result):
        log_attr = getattr(src, "tool_call_log", None)
        if isinstance(log_attr, list):
            return log_attr
    calls = getattr(result, "tool_calls", None)
    if isinstance(calls, list):
        return calls
    return []


async def _prepare_review_workspace(
    *,
    child_id: int,
    repo_url: str,
    parent_branch: str,
    user_id: int | None,
    organization_id: int | None,
) -> str:
    """Clone (or reuse) the child's workspace at the integration branch.

    Reuses the path ``agent.workspace.clone_repo`` allocates for
    ``task_id=child_id`` — which is the same dir coding/verify just used,
    so the child's branch is already checked out with its commits on
    top of the integration branch. The diff is visible without any
    further checkout.

    Falls through to a fresh clone if the workspace is gone (e.g. after
    cleanup). The reviewer doesn't strictly need the child's local
    commits — the child branch was pushed by coding's ``push_branch``
    before verify ran — but reusing avoids a duplicate clone and keeps
    the diff visible against the integration branch.
    """
    from agent.workspace import clone_repo

    return await clone_repo(
        repo_url,
        child_id,
        parent_branch,
        user_id=user_id,
        organization_id=organization_id,
    )


def _create_reviewer_agent(
    workspace: str,
    task_id: int,
    task_description: str,
    repo_name: str | None,
    home_dir: str | None,
    org_id: int | None,
):
    """Build the ``AgentLoop`` for the trio reviewer."""
    session_id = _fresh_session_id(task_id, "trio-review")
    agent = create_agent(
        workspace=workspace,
        session_id=session_id,
        task_id=task_id,
        task_description=task_description,
        with_browser=True,  # optional spot-check via browse_url
        max_turns=30,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    agent.system_prompt_override = TRIO_REVIEWER_SYSTEM
    return agent


async def handle_trio_review(
    child_task_id: int,
    *,
    workspace: str | None = None,
    parent_branch: str | None = None,
) -> None:
    """Run the reviewer for one builder cycle and act on its verdict.

    ``workspace`` and ``parent_branch`` are optional plumbing for callers
    that already have a prepared workspace (verify's ``_pass_cycle``);
    when omitted the reviewer clones afresh at the parent's integration
    branch.
    """
    async with async_session() as session:
        child = (
            await session.execute(select(Task).where(Task.id == child_task_id))
        ).scalar_one()
        if child.parent_task_id is None:
            log.warning(
                "trio.review.skipped_non_trio_child", child_id=child_task_id,
            )
            return
        # Snapshot everything we'll need outside the session (the reviewer
        # run is long; we don't want to hold a connection or trip lazy
        # loads on a detached instance later).
        parent_id = child.parent_task_id
        child_description = child.description or child.title
        repo_name = child.repo.name if child.repo else None
        repo_url = child.repo.url if child.repo else None
        user_id = child.created_by_user_id
        org_id = child.organization_id
        home_dir = await home_dir_for_task(child)
        # If verify didn't hand us a parent_branch, derive it.
        effective_parent_branch = parent_branch or f"trio/{parent_id}"

    if workspace is None:
        if repo_url is None:
            # Cold-start parents have no repo; reviewer has nothing to clone.
            # The caller should not reach this path in practice — trio
            # children inherit the parent's repo_id when one exists.
            log.error(
                "trio.review.no_workspace_no_repo", child_id=child_task_id,
            )
            return
        workspace = await _prepare_review_workspace(
            child_id=child_task_id,
            repo_url=repo_url,
            parent_branch=effective_parent_branch,
            user_id=user_id,
            organization_id=org_id,
        )

    prompt = (
        f"== Work item description (also PR body) ==\n{child_description}\n\n"
        "Review the diff in this workspace against ARCHITECTURE.md and the "
        "work item.\n"
        f"Run `git diff {effective_parent_branch}...HEAD` to see what changed.\n"
        "End your message with the verdict JSON block."
    )
    agent = _create_reviewer_agent(
        workspace=workspace,
        task_id=child_task_id,
        task_description=child_description,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    run_result = await agent.run(prompt)
    output = _result_output(run_result)
    tool_calls = _result_tool_calls(run_result, agent)
    verdict = _extract_verdict(output)

    async with async_session() as session:
        child = (
            await session.execute(select(Task).where(Task.id == child_task_id))
        ).scalar_one()
        existing = (
            await session.execute(
                select(TrioReviewAttempt).where(
                    TrioReviewAttempt.task_id == child.id
                )
            )
        ).scalars().all()
        cycle = len(existing) + 1

        if verdict is None:
            feedback = (
                "Reviewer produced invalid JSON. Please re-state the changes "
                "made and re-trigger review."
            )
            session.add(TrioReviewAttempt(
                task_id=child.id,
                cycle=cycle,
                ok=False,
                feedback=feedback,
                tool_calls=tool_calls,
            ))
            from orchestrator.state_machine import transition
            await transition(
                session, child, TaskStatus.CODING,
                message="trio review: invalid reviewer JSON",
            )
            await session.commit()
            log.info(
                "trio.review.invalid_json",
                child_id=child_task_id, cycle=cycle,
            )
            return

        ok = bool(verdict.get("ok"))
        feedback = str(verdict.get("feedback", ""))
        session.add(TrioReviewAttempt(
            task_id=child.id,
            cycle=cycle,
            ok=ok,
            feedback=feedback,
            tool_calls=tool_calls,
        ))
        if not ok:
            from orchestrator.state_machine import transition
            await transition(
                session, child, TaskStatus.CODING,
                message=f"trio review failed (cycle {cycle}): {feedback[:200]}",
            )
        # On ok=true we DON'T transition here — _open_pr_and_advance handles
        # the push and PR creation, then independent review takes the task
        # through PR_CREATED → AWAITING_CI itself.
        await session.commit()

    log.info(
        "trio.review.complete",
        child_id=child_task_id, cycle=cycle, ok=ok,
    )

    if ok:
        # Hand off to the existing PR-opening path. The trio child's base
        # branch is the parent's integration branch (set by coding.py for
        # any task with parent_task_id) — so _open_pr_and_advance pushes
        # the feature branch and opens a PR back into trio/<parent_id>.
        from agent.lifecycle._orchestrator_api import get_task
        from agent.lifecycle.coding import _open_pr_and_advance

        task_data = await get_task(child_task_id)
        if task_data is None or not task_data.branch_name:
            log.error(
                "trio.review.cannot_open_pr_missing_branch",
                child_id=child_task_id,
            )
            return
        await _open_pr_and_advance(
            child_task_id,
            task_data,
            workspace,
            effective_parent_branch,
            task_data.branch_name,
        )


# ---------------------------------------------------------------------------
# Heavy per-item reviewer — ADR-015 §3 / Phase 7.
# ---------------------------------------------------------------------------


@dataclass
class HeavyReviewResult:
    """Outcome of one heavy-reviewer pass.

    Mirrors the on-disk ``reviews/<id>.json`` shape so writes and reads
    round-trip losslessly.
    """

    verdict: str  # "pass" or "fail"
    alignment: str = ""
    smoke: str = ""
    ui: str = ""
    reason: str = ""


async def _load_item_diff(workspace: str, base_sha: str) -> str:
    """Return the unified diff from ``base_sha`` to current HEAD."""

    res = await sh.run(
        ["git", "diff", f"{base_sha}..HEAD"],
        cwd=workspace,
        timeout=30,
        max_output=200_000,
    )
    if res.failed:
        log.warning(
            "trio.heavy_review.git_diff_failed",
            base_sha=base_sha,
            stderr=(res.stderr or "")[:300],
        )
        return ""
    return res.stdout or ""


_ALIGNMENT_SYSTEM = (
    "You are the alignment-check reviewer. Given a backlog item spec and "
    "the unified diff that a builder produced for it, answer in one short "
    "paragraph: does the diff match the spec? "
    "If yes, start your reply with 'PASS:'. "
    "If no, start your reply with 'FAIL:' and name the specific mismatch."
)


def _alignment_prompt(item: dict, diff: str, grill_output: str) -> str:
    title = item.get("title", "(untitled)")
    description = item.get("description", "")
    justification = item.get("justification", "")
    affected_routes = item.get("affected_routes") or []
    diff_preview = diff if len(diff) < 30_000 else diff[:30_000] + "\n... (truncated)"
    grill_block = f"\n\n== Original grill output ==\n{grill_output[:4000]}" if grill_output else ""
    return (
        f"== Item spec — {item.get('id', '')} ==\n"
        f"Title: {title}\nJustification: {justification}\n"
        f"Affected routes: {affected_routes}\n\nDescription:\n{description}\n"
        f"{grill_block}\n\n"
        f"== Diff to review ==\n```diff\n{diff_preview}\n```"
    )


async def _run_alignment_agent(
    *,
    workspace_root: str,
    item: dict,
    diff: str,
    grill_output: str,
    repo_name: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
) -> str:
    """Run a short LLM call to judge whether the diff matches the item spec.

    Returns prose. Convention: starts with ``PASS:`` or ``FAIL:`` so the
    caller can synthesise the verdict cheaply.
    """

    agent = create_agent(
        workspace=workspace_root,
        task_id=0,
        task_description=item.get("description") or item.get("title", ""),
        readonly=True,
        with_browser=False,
        max_turns=8,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    agent.system_prompt_override = _ALIGNMENT_SYSTEM
    result = await agent.run(_alignment_prompt(item, diff, grill_output))
    output = getattr(result, "output", None)
    if output is None:
        return str(result) if result is not None else ""
    return output


def _write_review_json(
    *,
    workspace_root: str,
    item_id: str,
    result: HeavyReviewResult,
) -> None:
    """Persist the verdict so the orchestrator can read it via the skills bridge."""

    rel = review_path(item_id)
    abs_path = os.path.join(workspace_root, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    payload = {
        "schema_version": "1",
        "verdict": result.verdict,
        "alignment": result.alignment,
        "smoke": result.smoke,
        "ui": result.ui,
        "reason": result.reason,
    }
    with open(abs_path, "w") as fh:
        json.dump(payload, fh, indent=2)


async def run_heavy_review(
    *,
    item: dict,
    workspace_root: str,
    base_sha: str,
    grill_output: str = "",
    repo_name: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
) -> HeavyReviewResult:
    """Run alignment + grep + smoke + UI for one backlog item.

    Order of checks (ADR-015 §3):

      1. Alignment — LLM judges whether the diff matches the item spec.
      2. Stub-grep — ``grep_diff_for_stubs(diff)`` blocks on
         ``raise NotImplementedError`` and friends in added lines.
      3. Smoke — boot the dev server and ``exercise_routes(union)`` where
         ``union = item.affected_routes | infer_routes_from_diff(diff)``.
      4. UI — ``inspect_ui`` for each UI route that returned 2xx.

    On first failure the function short-circuits, writes
    ``reviews/<id>.json`` with ``verdict="fail"`` + synthesised reason
    and returns. All-green → ``verdict="pass"``.
    """

    item_id = str(item.get("id") or "item")

    diff = await _load_item_diff(workspace_root, base_sha)

    # 1. Alignment ----------------------------------------------------------
    alignment_text = await _run_alignment_agent(
        workspace_root=workspace_root,
        item=item,
        diff=diff,
        grill_output=grill_output,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    alignment_pass = not alignment_text.strip().upper().startswith("FAIL")
    if not alignment_pass:
        result = HeavyReviewResult(
            verdict="fail",
            alignment=alignment_text[:500],
            reason=(
                f"alignment check rejected the diff against the item spec: "
                f"{alignment_text.strip()[:400]}"
            ),
        )
        _write_review_json(workspace_root=workspace_root, item_id=item_id, result=result)
        return result

    # 2. Stub-grep ----------------------------------------------------------
    stubs = grep_diff_for_stubs(diff)
    blocking = [v for v in stubs.violations if not v.allowed_via_optout]
    if blocking:
        v = blocking[0]
        result = HeavyReviewResult(
            verdict="fail",
            alignment=alignment_text[:200] or "pass",
            reason=(
                f"no-defer stub detected in added line: {v.pattern} at "
                f"{v.file}:{v.line} — {v.snippet.strip()[:200]}"
            ),
        )
        _write_review_json(workspace_root=workspace_root, item_id=item_id, result=result)
        return result

    # 3. Smoke --------------------------------------------------------------
    declared_routes = list(item.get("affected_routes") or [])
    inferred_routes = infer_routes_from_diff(diff)
    union_routes: list[str] = []
    seen: set[str] = set()
    for r in declared_routes + inferred_routes:
        if r and r not in seen:
            union_routes.append(r)
            seen.add(r)

    smoke_summary = "n/a"
    ui_summary = "n/a"
    handle: ServerHandle | None = None
    try:
        if union_routes:
            handle = await boot_dev_server(workspace=workspace_root)
            if handle.state == "running":
                route_results = await exercise_routes(union_routes, handle=handle)
                failed_routes = {r: rr for r, rr in route_results.items() if not rr.ok}
                if failed_routes:
                    first = next(iter(failed_routes.items()))
                    route, rr = first
                    result = HeavyReviewResult(
                        verdict="fail",
                        alignment=alignment_text[:200] or "pass",
                        smoke=(
                            f"smoke: route {route} returned status={rr.status}, "
                            f"reason={rr.reason!r}"
                        ),
                        reason=(
                            f"smoke check failed for {route}: status={rr.status}, "
                            f"reason={rr.reason!r}"
                        ),
                    )
                    _write_review_json(
                        workspace_root=workspace_root, item_id=item_id, result=result
                    )
                    return result
                smoke_summary = f"smoke: {len(route_results)} route(s) returned 2xx"
            elif handle.state == "failed":
                result = HeavyReviewResult(
                    verdict="fail",
                    alignment=alignment_text[:200] or "pass",
                    smoke=f"smoke: dev server boot failed — {handle.failure_reason}",
                    reason=(
                        f"smoke: dev server boot failed — {handle.failure_reason}; "
                        f"cannot exercise affected routes {union_routes!r}."
                    ),
                )
                _write_review_json(
                    workspace_root=workspace_root, item_id=item_id, result=result
                )
                return result
            else:
                smoke_summary = "smoke: skipped (no boot config)"

            # 4. UI ---------------------------------------------------------
            if handle.state == "running":
                ui_failures: list[tuple[str, str]] = []
                for route in union_routes:
                    if not is_ui_route(route):
                        continue
                    ui = await inspect_ui(
                        route=route,
                        intent=item.get("description") or item.get("title", ""),
                        base_url=handle.base_url,
                    )
                    if not ui.ok:
                        if "playwright_not_installed" in (ui.reason or ""):
                            # Advisory — Phase 4 precedent in pr_reviewer.
                            log.info(
                                "trio.heavy_review.ui_inspection_skipped",
                                route=route,
                                reason=ui.reason,
                            )
                            continue
                        ui_failures.append((route, ui.reason))
                if ui_failures:
                    route, reason = ui_failures[0]
                    result = HeavyReviewResult(
                        verdict="fail",
                        alignment=alignment_text[:200] or "pass",
                        smoke=smoke_summary,
                        ui=f"ui: failed for {route} — {reason}",
                        reason=(
                            f"UI inspection failed for {route}: {reason}"
                        ),
                    )
                    _write_review_json(
                        workspace_root=workspace_root, item_id=item_id, result=result
                    )
                    return result
                ui_summary = (
                    "ui: all UI routes passed"
                    if any(is_ui_route(r) for r in union_routes)
                    else "n/a"
                )
        else:
            smoke_summary = "smoke: no routes declared or inferred"
    finally:
        if handle is not None:
            await handle.teardown()

    # All-green path --------------------------------------------------------
    result = HeavyReviewResult(
        verdict="pass",
        alignment=alignment_text.strip()[:200] or "pass",
        smoke=smoke_summary,
        ui=ui_summary,
        reason=(
            "alignment, no-defer stub-grep, smoke, and UI checks all passed."
        ),
    )
    _write_review_json(workspace_root=workspace_root, item_id=item_id, result=result)
    return result


__all__ = [
    "HeavyReviewResult",
    "handle_trio_review",
    "run_heavy_review",
]
