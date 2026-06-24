"""One health-fix cycle: code a batch of findings, gate it, stage onto cleanup.

Self-contained — driven inline by the supervisor while it holds the VM-global
lease, so a health fix never enters the normal task dispatcher. Given a repo, a
cleanup-branch name, and a batch of ranked findings it:

  1. clones the repo and cuts a fix branch off the **current cleanup tip**,
  2. runs the coder against an evidence-cited prompt,
  3. runs three gates that must ALL pass — differential (local before/after
     boot) + smoke (fail-closed runtime check) + CI green (the repo's existing
     CI on a PR targeting the cleanup branch),
  4. all pass → ``merge_fix`` into the cleanup branch (DONE); any fail → park
     the fix task FAILED with the reason and move on (no infinite retry).

The local gates run first so a bad fix fails fast without pushing a branch or
opening a PR. ``main`` is never touched — a human merges the standing
cleanup → main PR.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import structlog

from agent.health_loop.cleanup_branch import ensure_cleanup_branch, merge_fix
from agent.health_loop.differential import differential_verify
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.review import create_pr, find_existing_pr_url
from agent.lifecycle.route_inference import infer_routes_from_diff
from agent.lifecycle.trio.smoke_agent import run_smoke_agent
from agent.sh import run as sh_run
from agent.workspace import (
    clone_repo,
    commit_pending_changes,
    create_branch,
    push_branch,
)
from shared.database import async_session
from shared.github_auth import get_github_token
from shared.models import Task, TaskComplexity, TaskSource, TaskStatus

if TYPE_CHECKING:
    from agent.health_loop.findings import HealthFinding

log = structlog.get_logger()

# CI poll cadence for the fix PR. The fix PR targets the cleanup branch; we wait
# for the repo's existing CI to reach a terminal verdict. A repo with no CI on
# the PR yields "no_ci", which — consistent with the rest of the system
# (run.py::_pr_has_no_checks) — is treated as a pass.
_CI_POLL_INTERVAL = 30
_CI_POLL_TIMEOUT = 1800  # 30 min; overrun is treated as a CI failure (parks).

_CODER_MAX_TURNS = 50


@dataclass
class BatchOutcome:
    """Terminal result of one batch cycle.

    ``status`` is ``"merged"`` (staged onto cleanup), ``"parked"`` (a gate
    rejected it or a conflict — a real verdict, don't retry), ``"noop"`` (the
    coder changed nothing), or ``"error"`` (an infra/transient failure — the
    caller should NOT mark the findings addressed, so they retry). ``cleanup_pr_url``
    is set only on a merge, when the standing cleanup → main PR is ensured.
    """

    status: str
    detail: str = ""
    finding_hashes: list[str] = field(default_factory=list)
    fix_pr_url: str = ""
    cleanup_pr_url: str = ""


def _batch_hash(member_hashes: list[str]) -> str:
    """Stable 12-char id for a batch, order-independent across its members."""
    canonical = "|".join(sorted(member_hashes))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _build_fix_prompt(repo_name: str, batch: list[HealthFinding]) -> str:
    """Render an evidence-cited, behavior-preserving refactor brief."""
    lines = [
        f"You are performing an automated **health cleanup** on `{repo_name}`.",
        "",
        "Fix EVERY finding below. These are mechanical code-health issues, not "
        "feature work. Your change MUST be **behavior-preserving** — same routes, "
        "same responses, same UI. A differential before/after check will reject "
        "any observable change.",
        "",
        "## Findings to fix",
    ]
    for i, f in enumerate(batch, 1):
        files = ", ".join(f.files) if f.files else "(no file)"
        lines.append(f"{i}. [{f.category}] {f.title}")
        lines.append(f"   files: {files}")
    lines += [
        "",
        "## Rules",
        "- Make the smallest change that resolves each finding.",
        "- Do NOT add features, change public behavior, or alter response shapes.",
        "- Run the repo's tests/build before finishing; fix anything you break.",
        "- Commit your work.",
    ]
    return "\n".join(lines)


async def _create_fix_task(
    *,
    repo_id: int,
    repo_name: str,
    organization_id: int,
    batch: list[HealthFinding],
    batch_hash: str,
    fix_branch: str,
    description: str,
    created_by_user_id: int | None,
) -> int:
    """Create the visibility/record Task for this batch and return its id.

    Lives OUTSIDE the normal lifecycle: status is set directly (never through
    the dispatcher or state machine) and kept in VERIFYING — a status no poller
    or watchdog scans and which does not occupy a concurrency slot — until it
    reaches DONE or FAILED here. ``created_by_user_id`` is the user who started
    the loop; it's what lets ``home_dir_for_task`` resolve their paired Claude
    credentials for the coder (there is no shared host Claude in prod).
    """
    async with async_session() as session:
        task = Task(
            title=f"Auto-heal: {len(batch)} finding(s) in {repo_name}",
            description=description,
            source=TaskSource.FREEFORM,
            source_id=f"health:{repo_id}:batch:{batch_hash}",
            status=TaskStatus.VERIFYING,
            complexity=TaskComplexity.COMPLEX,
            repo_id=repo_id,
            organization_id=organization_id,
            branch_name=fix_branch,
            freeform_mode=False,
            created_by_user_id=created_by_user_id,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def _finalize(task_id: int, status: TaskStatus, detail: str, pr_url: str = "") -> None:
    """Set the fix task's terminal status + reason directly (out-of-band)."""
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return
        task.status = status
        if status == TaskStatus.FAILED:
            task.error = detail[:2000]
        if pr_url:
            task.pr_url = pr_url
        await session.commit()


async def _set_pr_url(task_id: int, pr_url: str) -> None:
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task is not None:
            task.pr_url = pr_url
            await session.commit()


async def _diff_against(workspace: str, base_branch: str) -> str:
    """Return the unified diff of the current branch against ``base_branch``."""
    result = await sh_run(["git", "diff", base_branch, "HEAD"], cwd=workspace, timeout=60)
    return result.stdout


def _parse_pr_url(pr_url: str) -> tuple[str, str, str]:
    parts = pr_url.rstrip("/").split("/")
    return parts[-4], parts[-3], parts[-1]


async def _poll_ci(pr_url: str, organization_id: int) -> str:
    """Poll the fix PR's CI to a terminal verdict.

    Returns ``"success"``, ``"failure"``, or ``"no_ci"`` (no checks configured —
    treated as a pass by the caller). Times out to ``"failure"`` so a hung CI
    parks the fix rather than wedging the loop.
    """
    token = await get_github_token(organization_id=organization_id)
    if not token:
        return "no_ci"
    owner, repo, num = _parse_pr_url(pr_url)
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    waited = 0
    async with httpx.AsyncClient() as client:
        while waited <= _CI_POLL_TIMEOUT:
            verdict = await _ci_verdict_once(client, owner, repo, num, headers)
            if verdict is not None:
                return verdict
            await asyncio.sleep(_CI_POLL_INTERVAL)
            waited += _CI_POLL_INTERVAL
    return "failure"


async def _ci_verdict_once(client, owner, repo, num, headers) -> str | None:
    """One CI poll. Returns a terminal verdict, ``"no_ci"``, or None (pending)."""
    pr = await client.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}", headers=headers
    )
    if pr.status_code != 200:
        return None
    head_sha = pr.json()["head"]["sha"]
    runs_resp = await client.get(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
        headers=headers,
    )
    if runs_resp.status_code != 200:
        return None
    check_runs = runs_resp.json().get("check_runs", [])
    if not check_runs:
        return "no_ci"
    statuses = [cr.get("status") for cr in check_runs]
    if any(s != "completed" for s in statuses):
        return None  # still running
    conclusions = [cr.get("conclusion") for cr in check_runs]
    if any(c in ("failure", "timed_out", "action_required") for c in conclusions):
        return "failure"
    return "success"


async def _ensure_cleanup_pr(
    *, workspace: str, cleanup_branch: str, base_branch: str, organization_id: int
) -> str:
    """Ensure a standing cleanup → main PR exists; return its URL ("" on failure).

    Best-effort: the merge already landed on the cleanup branch, so a failure to
    open/find the umbrella PR must not flip the batch outcome to parked.
    """
    try:
        existing = await find_existing_pr_url(
            workspace, cleanup_branch, organization_id=organization_id
        )
        if existing:
            return existing
        return await create_pr(
            workspace,
            "Auto-heal: code-graph health cleanup",
            "Standing PR — accumulates behavior-preserving health fixes staged "
            "by the auto-heal loop. Review and merge on your own cadence.",
            base_branch=base_branch,
            head_branch=cleanup_branch,
            organization_id=organization_id,
        )
    except Exception:
        log.warning("health_loop.cleanup_pr_failed", exc_info=True)
        return ""


async def run_batch(
    *, repo, cleanup_branch: str, batch: list[HealthFinding], started_by_user_id: int | None = None
) -> BatchOutcome:
    """Run one full fix cycle for ``batch`` and return its terminal outcome.

    ``repo`` is a ``Repo`` ORM row (id, name, url, default_branch,
    organization_id). ``started_by_user_id`` is the user who enabled the loop;
    the coder runs as them so it uses their paired Claude + GitHub credentials.
    The caller (supervisor) records the outcome into the config.
    """
    member_hashes = [f.finding_hash for f in batch]
    batch_hash = _batch_hash(member_hashes)
    prompt = _build_fix_prompt(repo.name, batch)
    fix_branch = f"health/{repo.id}/batch-{batch_hash}"
    org_id = repo.organization_id

    def parked(detail: str) -> BatchOutcome:
        return BatchOutcome("parked", detail, member_hashes, fix_pr_url=fix_pr_url)

    fix_pr_url = ""
    task_id: int | None = None
    try:
        task_id = await _create_fix_task(
            repo_id=repo.id,
            repo_name=repo.name,
            organization_id=org_id,
            batch=batch,
            batch_hash=batch_hash,
            fix_branch=fix_branch,
            description=prompt,
            created_by_user_id=started_by_user_id,
        )
        log.info(
            "health_loop.batch_started",
            repo_id=repo.id,
            task_id=task_id,
            batch_hash=batch_hash,
            fix_branch=fix_branch,
            user_id=started_by_user_id,
        )

        # 1. Clone + cut the fix branch off the current cleanup tip.
        fix_ws = await clone_repo(
            repo.url,
            task_id,
            repo.default_branch,
            user_id=started_by_user_id,
            organization_id=org_id,
            repo_id=repo.id,
        )
        await ensure_cleanup_branch(
            workspace=fix_ws, base_branch=repo.default_branch, cleanup_branch=cleanup_branch
        )
        await create_branch(fix_ws, fix_branch)
        log.info("health_loop.workspace_ready", task_id=task_id, workspace=fix_ws)

        # 2. Run the coder.
        task_obj = await _load_task(task_id)
        home_dir = await home_dir_for_task(task_obj)
        log.info("health_loop.coder_start", task_id=task_id, has_home_dir=home_dir is not None)
        agent = create_agent(
            fix_ws,
            max_turns=_CODER_MAX_TURNS,
            task_id=task_id,
            task_description=f"Auto-heal {len(batch)} finding(s) in {repo.name}",
            repo_name=repo.name,
            home_dir=home_dir,
            org_id=org_id,
            repo_id=repo.id,
        )
        await agent.run(prompt)

        # 3. Commit any work the coder left UNcommitted (safety net), then
        # judge by the branch-vs-cleanup-tip diff — the real signal. The coder
        # often commits its own changes, so the tree can be clean while the
        # branch is NOT empty; gating on "pending changes" alone misreads that
        # as a no-op and silently discards a real fix.
        await commit_pending_changes(fix_ws, task_id, f"health cleanup batch {batch_hash}")
        diff = await _diff_against(fix_ws, cleanup_branch)
        if not diff.strip():
            log.info("health_loop.no_changes", task_id=task_id)
            await _finalize(task_id, TaskStatus.FAILED, "coder produced no changes")
            return BatchOutcome("noop", "no diff", member_hashes)
        log.info("health_loop.coder_done", task_id=task_id, diff_chars=len(diff))

        # 4a. Differential gate (local before/after boot) — cheapest signal first.
        base_ws = await clone_repo(
            repo.url,
            0,
            cleanup_branch,
            workspace_name=f"health-base-{repo.id}",
            user_id=started_by_user_id,
            organization_id=org_id,
            repo_id=repo.id,
        )
        routes = infer_routes_from_diff(diff)
        diff_result = await differential_verify(
            base_workspace=base_ws,
            branch_workspace=fix_ws,
            routes=routes,
            repo_id=repo.id,
        )
        log.info(
            "health_loop.gate_differential",
            task_id=task_id,
            regressed=diff_result.regressed,
            routes=len(routes),
        )
        if diff_result.regressed:
            reason = diff_result.note or "; ".join(d.detail for d in diff_result.diffs)
            await _finalize(task_id, TaskStatus.FAILED, f"differential regression: {reason}")
            return parked(f"differential: {reason}")

        # 4b. Smoke gate (fail-closed runtime check).
        smoke = await run_smoke_agent(
            workspace_root=fix_ws,
            item=None,
            design=prompt,
            diff=diff,
            repo_name=repo.name,
            home_dir=home_dir,
            org_id=org_id,
            repo_id=repo.id,
            task_id=task_id,
        )
        log.info("health_loop.gate_smoke", task_id=task_id, verdict=smoke.verdict)
        if smoke.verdict != "pass":
            await _finalize(task_id, TaskStatus.FAILED, f"smoke failed: {smoke.summary[:400]}")
            return parked(f"smoke: {smoke.summary[:200]}")

        # 4c. CI green — push the fix branch, open a PR to the cleanup branch,
        # and wait for the repo's existing CI.
        await push_branch(fix_ws, fix_branch)
        fix_pr_url = await create_pr(
            fix_ws,
            f"Auto-heal: {len(batch)} finding(s) [{batch_hash}]",
            prompt,
            base_branch=cleanup_branch,
            head_branch=fix_branch,
            user_id=started_by_user_id,
            organization_id=org_id,
        )
        await _set_pr_url(task_id, fix_pr_url)
        ci = await _poll_ci(fix_pr_url, org_id)
        log.info("health_loop.gate_ci", task_id=task_id, verdict=ci, pr_url=fix_pr_url)
        if ci == "failure":
            await _finalize(task_id, TaskStatus.FAILED, "CI failed", pr_url=fix_pr_url)
            return parked("CI failed")

        # 5. All gates passed → stage onto the cleanup branch.
        merged = await merge_fix(
            workspace=fix_ws, fix_branch=fix_branch, cleanup_branch=cleanup_branch
        )
        if not merged:
            await _finalize(
                task_id, TaskStatus.FAILED, "merge conflict onto cleanup branch", pr_url=fix_pr_url
            )
            return parked("merge conflict")

        cleanup_pr = await _ensure_cleanup_pr(
            workspace=fix_ws,
            cleanup_branch=cleanup_branch,
            base_branch=repo.default_branch,
            organization_id=repo.organization_id,
        )
        await _finalize(
            task_id, TaskStatus.DONE, f"merged into {cleanup_branch}", pr_url=fix_pr_url
        )
        return BatchOutcome(
            "merged",
            f"merged into {cleanup_branch}",
            member_hashes,
            fix_pr_url=fix_pr_url,
            cleanup_pr_url=cleanup_pr,
        )
    except Exception as e:
        import traceback as _tb

        log.error(
            "health_loop.batch_error",
            task_id=task_id,
            batch_hash=batch_hash,
            error=str(e),
            error_type=type(e).__name__,
            tb=_tb.format_exc(),
        )
        if task_id is not None:
            await _finalize(task_id, TaskStatus.FAILED, f"batch handler error: {e}")
        # "error" (not "parked"): an infra/transient failure is not a gate
        # verdict, so the supervisor leaves these findings un-addressed to retry.
        return BatchOutcome("error", f"error: {e}", member_hashes, fix_pr_url=fix_pr_url)


async def _load_task(task_id: int) -> Task | None:
    async with async_session() as session:
        return await session.get(Task, task_id)
