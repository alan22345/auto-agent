"""Cleanup-branch lifecycle for the auto-heal loop.

Owns the long-lived branch that stages accepted health fixes. Operations:
create-if-missing, rebase-onto-base (keeping the branch current with
``main``), and merge-accepted-fix. The branch is never auto-merged to the
base — a human reviews and merges it.

This is orchestration-layer git (``agent.sh.run``), like
:mod:`agent.workspace` — NOT the sandboxed in-agent ``agent/tools/git.py``.
The one deliberately-loosened guardrail (force-push) is constrained here by
:func:`_force_push_cleanup`, which refuses any branch outside an explicit
allowlist so a bug can never force-push ``main`` or a user branch.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent import sh

DEFAULT_CLEANUP_BRANCH = "auto-agent/health-cleanup"


@dataclass
class RebaseOutcome:
    """Result of :func:`rebase_onto_base`.

    ``ok`` True ⇒ rebased clean and force-pushed. ``conflict`` True ⇒ the
    rebase hit a conflict, was aborted, and the branch is unchanged (the
    caller parks it for human resolution).
    """

    ok: bool
    conflict: bool = False
    detail: str = ""


async def _git(*args: str, cwd: str, check: bool = True):
    """Run a git command via the shared subprocess seam.

    Returns the ``sh`` result (``.failed``/``.stdout``/``.stderr``). Raises
    ``RuntimeError`` on failure when ``check`` is True.
    """
    result = await sh.run(["git", *args], cwd=cwd, timeout=60)
    if check and result.failed:
        raise RuntimeError(
            f"git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


async def _force_push_cleanup(
    *, workspace: str, cleanup_branch: str, allowed_branches: set[str]
) -> None:
    """Force-push ``cleanup_branch`` to origin — ONLY if allowlisted.

    The single deliberately-loosened guardrail in the auto-heal loop. The
    allowlist check runs before any git command so a misconfigured caller
    can never force-push a non-cleanup branch. Its teeth come from the
    CALLER passing a FIXED allowlist (e.g. ``{DEFAULT_CLEANUP_BRANCH}``): the
    guard catches a misconfigured force-push *target*, so callers must pass an
    explicit allowlist rather than one derived from the ``cleanup_branch``
    argument (which would make the check tautological).
    """
    if cleanup_branch not in allowed_branches:
        raise ValueError(
            f"refusing to force-push {cleanup_branch!r}: not in the cleanup "
            f"allowlist {sorted(allowed_branches)}"
        )
    await _git("push", "--force-with-lease", "origin", cleanup_branch, cwd=workspace)


async def ensure_cleanup_branch(*, workspace: str, base_branch: str, cleanup_branch: str) -> None:
    """Check out the cleanup branch, creating it off ``base_branch`` if it
    doesn't exist yet on the remote. Idempotent.
    """
    await _git("fetch", "origin", cwd=workspace)
    existing = await _git(
        "ls-remote", "--heads", "origin", cleanup_branch, cwd=workspace, check=False
    )
    if existing.stdout.strip():
        await _git("checkout", "-B", cleanup_branch, f"origin/{cleanup_branch}", cwd=workspace)
    else:
        await _git("checkout", "-B", cleanup_branch, f"origin/{base_branch}", cwd=workspace)
        await _git("push", "-u", "origin", cleanup_branch, cwd=workspace)


async def merge_fix(*, workspace: str, fix_branch: str, cleanup_branch: str) -> bool:
    """Merge an accepted fix branch into the cleanup branch and push.

    Returns True on a clean merge (pushed), False if the merge conflicted
    (aborted, cleanup branch unchanged). The push is an ordinary
    fast-forward of the cleanup branch — no force needed.
    """
    await _git("fetch", "origin", cwd=workspace)
    await _git("checkout", "-B", cleanup_branch, f"origin/{cleanup_branch}", cwd=workspace)
    merged = await _git(
        "merge",
        "--no-ff",
        "-m",
        f"health: merge {fix_branch}",
        fix_branch,
        cwd=workspace,
        check=False,
    )
    if merged.failed:
        await _git("merge", "--abort", cwd=workspace, check=False)
        return False
    await _git("push", "origin", cleanup_branch, cwd=workspace)
    return True


async def rebase_onto_base(
    *,
    workspace: str,
    base_branch: str,
    cleanup_branch: str,
    allowed_branches: set[str] | None = None,
) -> RebaseOutcome:
    """Rebase the cleanup branch onto the latest base, then force-push it.

    On a clean rebase the branch is force-pushed (via the allowlist guard)
    and ``ok=True``. On conflict the rebase is aborted, the branch is left
    unchanged, and ``conflict=True`` is returned for the caller to park.
    """
    # NOTE: falling back to ``{cleanup_branch}`` makes the allowlist guard a
    # no-op (the target always matches the allowlist). Real protection requires
    # the caller to pass an explicit allowlist (e.g. {DEFAULT_CLEANUP_BRANCH}).
    allowed = allowed_branches or {cleanup_branch}
    await _git("fetch", "origin", cwd=workspace)
    await _git("checkout", "-B", cleanup_branch, f"origin/{cleanup_branch}", cwd=workspace)
    rebased = await _git("rebase", f"origin/{base_branch}", cwd=workspace, check=False)
    if rebased.failed:
        await _git("rebase", "--abort", cwd=workspace, check=False)
        return RebaseOutcome(
            ok=False,
            conflict=True,
            detail=(rebased.stderr.strip() or rebased.stdout.strip())[:300] or "rebase conflict",
        )
    await _force_push_cleanup(
        workspace=workspace, cleanup_branch=cleanup_branch, allowed_branches=allowed
    )
    return RebaseOutcome(ok=True)
