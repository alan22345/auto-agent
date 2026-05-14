"""PR-reviewer agent role — ADR-015 §5 (Phases 4 + 5).

Two scopes:

- ``correctness`` (simple flow) — pure-Python pipeline running the shared
  :mod:`agent.lifecycle.verify_primitives` end-to-end against the PR
  diff. Implemented in Phase 4.
- ``artefact`` (complex / complex_large) — LLM-authored review of PR
  hygiene, commit narrative, description coherence, missing tests, and
  unrelated changes. Implemented in Phase 5 via the ``submit-pr-review``
  skill seam: the agent writes ``.auto-agent/pr_review.json`` and the
  orchestrator reads it after ``agent.run`` returns. Two-retry-then-
  escalate contract per ADR-015 §12 — missing file after the second
  attempt raises :class:`MissingPRReviewError`.

The verdict + comments land at the same on-disk path for both scopes
(``.auto-agent/pr_review.json``) so the orchestrator's gate-file reader
finds the same shape regardless of scope.

After an ``artefact``-scope review returns non-empty ``comments``, the
caller invokes :func:`address_own_comments` for **exactly one** coding
turn. Per ADR-015 §5: "the same agent addresses its own comments" in
one round — no second self-review.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from agent import sh
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.route_inference import (
    infer_routes_from_diff,
    is_ui_route,
)
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
from agent.lifecycle.workspace_reader import read_gate_file
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


class MissingPRReviewError(RuntimeError):
    """Raised when the agent did not write ``pr_review.json`` after retries.

    Per ADR-015 §12 the orchestrator retries the agent invocation once on
    missing-output, then escalates. Callers should catch this and route
    the task to ``BLOCKED`` (non-freeform) or to the standin (freeform —
    Phase 10).
    """


# Maximum agent retries on missing output, per ADR-015 §12.
_MAX_MISSING_OUTPUT_RETRIES = 2


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

    Used by the correctness-scope pipeline (pure Python). The artefact
    scope routes the write through the ``submit-pr-review`` skill, which
    the agent invokes during its turn; the orchestrator reads the same
    shape afterwards via :func:`workspace_reader.read_gate_file`.
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

    ``scope`` selects the pipeline:

    - ``"correctness"`` — pure-Python pipeline running the shared verify
      primitives end-to-end. Used by the simple flow as its only verify
      gate.
    - ``"artefact"`` — LLM-authored review of PR hygiene, commit
      narrative, and description coherence. The agent calls the
      ``submit-pr-review`` skill, which writes
      ``.auto-agent/pr_review.json``. The orchestrator reads it after
      ``agent.run`` returns. Missing-output triggers 1 retry then
      raises :class:`MissingPRReviewError`.
    """

    if scope == "artefact":
        return await _run_artefact_review(task=task, workspace_root=workspace_root)

    if scope == "correctness":
        return await _run_correctness_review(task=task, workspace_root=workspace_root)

    raise ValueError(f"unknown PR review scope: {scope!r}")


# ---------------------------------------------------------------------------
# Artefact scope implementation (ADR-015 §5 Phase 5).
# ---------------------------------------------------------------------------


# Lens the agent applies. The body of the prompt enumerates the
# artefact-scope checks from ADR-015 §5 so the LLM has a concrete rubric
# rather than an ambient "review the PR" handwave.
_ARTEFACT_REVIEW_PROMPT = """\
You are reviewing an open pull request as a careful teammate would, on
the PR artefact itself — not its correctness (the verify gate already
covered that for complex / complex_large flows). Read:

  - the PR title and description
  - the commit narrative (titles and bodies, in order)
  - the unified diff (below)
  - any CI signals attached to the task

Apply this rubric (ADR-015 §5):

  1. Does the PR description coherently describe the change? Does it
     mention every load-bearing decision in the diff (new flags,
     migrations, behavioural changes)?
  2. Do the commit titles tell a clean narrative? Are there any
     "wip"/"fix"/"oops"-style messages that should be squashed?
  3. Are there unrelated changes mixed in (drive-by edits in distant
     files, accidental reformats)?
  4. Are tests present for the added features? Missing-test PRs need a
     comment.
  5. If CI is failing, is there a clear path to green?

Output your verdict by calling the ``submit-pr-review`` skill. The skill
writes ``.auto-agent/pr_review.json`` with the verdict + comments. Use
``verdict="approved"`` only when the rubric passes; otherwise
``verdict="changes_requested"`` with one comment per item.

==== Task title ====
{task_title}

==== Task description ====
{task_description}

==== PR URL ====
{pr_url}

==== Unified diff ====
{diff}
"""


async def _run_artefact_review(
    *,
    task: Any,
    workspace_root: str,
) -> PRReviewResult:
    """LLM-driven artefact-scope PR review using the ``submit-pr-review`` skill.

    Two-attempt budget per ADR-015 §12 — the agent gets one retry to
    write ``pr_review.json``; the second miss raises
    :class:`MissingPRReviewError` so the caller can park the task.
    """

    base_branch = getattr(task, "base_branch", None) or "main"
    diff = await _load_pr_diff(workspace_root, base_branch=base_branch)

    prompt = _ARTEFACT_REVIEW_PROMPT.format(
        task_title=getattr(task, "title", "") or "",
        task_description=getattr(task, "description", "") or "",
        pr_url=getattr(task, "pr_url", "") or "",
        diff=diff,
    )

    # Clear any stale pr_review.json so we don't mis-attribute an old
    # verdict to this run. Important when the same workspace is reused
    # across retries.
    pr_review_abs = os.path.join(workspace_root, PR_REVIEW_PATH)
    if os.path.isfile(pr_review_abs):
        os.remove(pr_review_abs)

    last_output = ""
    for attempt in range(_MAX_MISSING_OUTPUT_RETRIES):
        agent = create_agent(
            workspace_root,
            readonly=True,  # artefact scope: read-only review
            max_turns=15,
            task_description=getattr(task, "description", None),
            repo_name=getattr(task, "repo_name", None),
            home_dir=await home_dir_for_task(task),
            org_id=getattr(task, "organization_id", None),
        )
        attempt_prompt = prompt
        if attempt > 0:
            # Skills-bridge missing-output prompt amendment per ADR-015 §12.
            attempt_prompt = (
                "Your previous response did not write the pr_review.json file. "
                "You MUST call the submit-pr-review skill before stopping.\n\n" + prompt
            )
        result = await agent.run(attempt_prompt)
        last_output = getattr(result, "output", "") or ""

        payload = read_gate_file(workspace_root, PR_REVIEW_PATH, schema_version="1")
        if isinstance(payload, dict):
            verdict = payload.get("verdict")
            if verdict not in ("approved", "changes_requested"):
                # Treat schema-shape failures as missing — retry once.
                log.warning(
                    "pr_review.artefact.bad_verdict",
                    attempt=attempt,
                    verdict=verdict,
                )
                continue
            comments = payload.get("comments") or []
            summary = payload.get("summary", "") or ""
            return PRReviewResult(
                verdict=verdict,
                comments=list(comments),
                summary=summary,
            )
        log.warning(
            "pr_review.artefact.missing_output",
            attempt=attempt,
            output_preview=last_output[:200],
        )

    raise MissingPRReviewError(
        f"agent did not write {PR_REVIEW_PATH} after "
        f"{_MAX_MISSING_OUTPUT_RETRIES} attempts; last output: {last_output[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Address-own-comments — exactly one coding turn (ADR-015 §5 Phase 5).
# ---------------------------------------------------------------------------


_ADDRESS_COMMENTS_PROMPT = """\
Address these PR review comments by pushing fix-up commits or updating
the PR description as appropriate. After this turn the user reviews —
there is no second self-review pass, so make this round count.

If a comment is purely about the PR description, edit the PR body (e.g.
``gh pr edit --body ...``). If it's about the code, write or amend the
relevant files and commit. If a comment turns out to be wrong on closer
look, leave a reply explaining why — do not silently ignore it.

Comments:

{comments}
"""


async def address_own_comments(
    *,
    task: Any,
    workspace_root: str,
    comments: list[dict[str, Any]],
) -> None:
    """Run exactly ONE coding turn to address the PR-review comments.

    No-op when ``comments`` is empty. The "one round bound" is enforced
    by the structure of this function — it dispatches a single
    ``agent.run`` call and returns. Callers that want a second round
    must explicitly call it again, but ADR-015 §5 forbids that.
    """

    if not comments:
        return

    rendered_comments = "\n".join(
        f"- {_render_comment(c)}" for c in comments if isinstance(c, dict)
    )
    prompt = _ADDRESS_COMMENTS_PROMPT.format(comments=rendered_comments)

    agent = create_agent(
        workspace_root,
        max_turns=30,
        task_id=getattr(task, "id", None),
        task_description=getattr(task, "description", None),
        repo_name=getattr(task, "repo_name", None),
        home_dir=await home_dir_for_task(task),
        org_id=getattr(task, "organization_id", None),
    )
    result = await agent.run(prompt)
    log.info(
        "pr_review.address_own_comments.complete",
        comments_count=len(comments),
        output_preview=(getattr(result, "output", "") or "")[:200],
    )


def _render_comment(c: dict[str, Any]) -> str:
    path = c.get("path")
    line = c.get("line")
    text = c.get("comment", "")
    if path and line is not None:
        return f"{path}:{line} — {text}"
    if path:
        return f"{path} — {text}"
    return str(text)


# ---------------------------------------------------------------------------
# Correctness scope implementation (Phase 4 — unchanged).
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
                if not is_ui_route(route) or not rr.ok:
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
