"""Sub-architect dispatch + parent-grill relay — ADR-015 §9 / §10 / Phase 8.

When the parent architect emits a ``spawn_sub_architects`` decision the
orchestrator delegates the per-slice work to this module. Each slice
runs as its own architect agent inside ``.auto-agent/slices/<name>/``,
producing a slice-scoped design + backlog + per-item builder/reviewer
cycle. Sub-architects run **serially** so the parent's persisted
session stays free to answer grill questions (§10).

Two seams compose to form the dispatcher:

  * :func:`dispatch_sub_architects` — the public entry point. Walks
    slices in declared order and aggregates results. Enforces the
    1-level recursion bound (a sub-architect that itself emits
    ``spawn_sub_architects`` is rejected post-hoc).

  * :func:`_run_sub_architect_slice` — runs one slice through its own
    design → backlog → builder → review loop, with the parent-grill
    relay handled in-line. Patched out in tests so the per-slice
    runtime can be exercised independently.

The parent-grill relay is also extracted as a seam — :func:`_ask_parent_to_answer_grill`
— so tests can mock it without standing up the full architect-resume
machinery.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import structlog

from agent.lifecycle.workspace_paths import (
    slice_decision_path,
    slice_dir,
    slice_grill_answer_path,
    slice_grill_question_path,
)
from agent.lifecycle.workspace_reader import read_gate_file

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Public dataclasses.
# ---------------------------------------------------------------------------


@dataclass
class SliceResult:
    """Outcome of running one slice through its sub-architect.

    Attributes:
        name: The slice name from the parent architect's decision.
        status: ``"completed"`` (slice drained successfully) or
            ``"failed"`` (permanent failure — recursion bound, runaway
            grill loop, builder failure).
        reason: Free-form reason on failure; empty on completion.
    """

    name: str
    status: str  # "completed" or "failed"
    reason: str | None = None


@dataclass
class SubArchitectDispatchResult:
    """Aggregated outcome the trio orchestrator acts on.

    ``ok=True`` only when every slice completed. Callers use
    ``blocked_reason`` (the first slice's failure reason, prefixed with
    the slice name) when persisting the parent task's BLOCKED state.
    """

    ok: bool
    slice_results: list[SliceResult] = field(default_factory=list)
    blocked_reason: str | None = None


# ---------------------------------------------------------------------------
# Bounds.
# ---------------------------------------------------------------------------


# Maximum grill round-trips per slice — protects against a runaway
# sub-architect that never stops asking. Generous: the cap exists to
# break loops, not to limit reasonable clarification.
MAX_GRILL_ROUNDS_PER_SLICE = 6


# ---------------------------------------------------------------------------
# Validation — the structural enforcement point for the 1-level recursion
# bound. Decoupled so existing decision validators can compose this
# without circular imports.
# ---------------------------------------------------------------------------


def validate_decision_for_role(
    *,
    decision: dict[str, Any],
    is_sub_architect: bool,
) -> tuple[bool, str | None]:
    """Reject ``spawn_sub_architects`` when the architect is a sub-architect.

    Returns ``(ok, reason)`` — ``reason`` is non-empty on rejection
    so the caller can surface it back to the architect (or persist it
    on the parent task).

    The parent architect can spawn sub-architects (no recursion); a
    sub-architect emitting the same action is the 1-level bound being
    breached (ADR-015 §9). The check lives here, not in
    ``architect_decision.read_decision``, because that read-side is
    role-agnostic by design.
    """

    if not isinstance(decision, dict):
        return False, "decision must be a JSON object"

    action = decision.get("action")
    if not isinstance(action, str):
        return False, "decision must carry a string 'action'"

    if action == "spawn_sub_architects" and is_sub_architect:
        return False, (
            "1-level recursion bound: sub-architects cannot spawn "
            "sub-sub-architects. Either coarsen your slice into one flat "
            "backlog or escalate via 'escalate' to the parent."
        )

    return True, None


# ---------------------------------------------------------------------------
# Per-slice runtime — the actual sub-architect run.
#
# Tests patch this seam to inject a fake runner; it stays a top-level
# coroutine so the patch target is stable.
# ---------------------------------------------------------------------------


async def _run_sub_architect_slice(
    *,
    parent_task: Any,
    slice_spec: dict[str, Any],
    workspace_root: str,
    slice_root: str,
    **_kwargs: Any,
) -> dict[str, Any]:  # pragma: no cover — production path stubbed in Phase 8
    """Run one slice's full design → backlog → builder/review cycle.

    Returns a dict with at minimum ``status`` ∈ {``"completed"``,
    ``"paused_for_grill"``, ``"failed"``}. ``"paused_for_grill"``
    indicates a grill question was written and the dispatcher should
    relay it to the parent before re-invoking this runner.

    The default production implementation is deferred — Phase 8 ships
    the orchestration; the per-slice runtime is wired up in tests via
    monkey-patching and in production by a follow-up that composes
    :mod:`agent.lifecycle.trio.architect` (run_design, finalize_design,
    backlog emit) against the slice-scoped paths.
    """

    log.warning(
        "trio.sub_architect.runtime_not_wired",
        slice_name=slice_spec.get("name"),
        slice_root=slice_root,
    )
    return {
        "status": "failed",
        "reason": (
            "sub-architect per-slice runtime is not wired in Phase 8; "
            "tests patch _run_sub_architect_slice. Wire-up lands when "
            "the architect entry points are composed against slice "
            "paths."
        ),
    }


# ---------------------------------------------------------------------------
# Parent-grill relay seam — patched in tests; in production it resumes
# the parent's persisted session and runs the submit-grill-answer skill.
# ---------------------------------------------------------------------------


async def _ask_parent_to_answer_grill(
    *,
    parent_task: Any,
    slice_name: str,
    question: str,
    workspace_root: str,
) -> None:  # pragma: no cover — production path stubbed in Phase 8
    """Resume the parent architect's session to answer a grill question.

    The contract: when this function returns, ``slices/<name>/grill_answer.json``
    is on disk under ``workspace_root``. Tests patch this to write the
    answer file directly. Production wiring resumes the parent's
    persisted Session (see ``agent.lifecycle.trio.gap_fix`` for the
    same resume pattern) and re-invokes the architect with a prompt
    that points at the ``submit-grill-answer`` skill.

    The production stub leaves the answer file absent so the
    dispatcher falls into its grill-round cap and the slice fails
    safely until the production wire-up lands.
    """

    log.warning(
        "trio.sub_architect.parent_grill_runtime_not_wired",
        slice_name=slice_name,
        question_preview=question[:80],
    )


# ---------------------------------------------------------------------------
# Helpers — slice workspace + path bookkeeping.
# ---------------------------------------------------------------------------


def _ensure_slice_dir(workspace_root: str, slice_name: str) -> str:
    """Create ``.auto-agent/slices/<name>/`` and return the absolute path."""

    abs_path = os.path.join(workspace_root, slice_dir(slice_name))
    os.makedirs(abs_path, exist_ok=True)
    return abs_path


def _remove_relay_file(workspace_root: str, relative_path: str, slice_name: str) -> None:
    """Best-effort removal of a single relay file."""

    abs_path = os.path.join(workspace_root, relative_path)
    if os.path.isfile(abs_path):
        try:
            os.remove(abs_path)
        except OSError as exc:  # pragma: no cover — best effort
            log.warning(
                "trio.sub_architect.relay_cleanup_failed",
                slice=slice_name,
                path=abs_path,
                error=str(exc),
            )


def _clear_stale_relay_files(workspace_root: str, slice_name: str) -> None:
    """At the start of a slice, drop any leftover relay files from a
    prior parent task run (or a half-finished previous attempt).

    Once the relay is in progress the dispatcher manages each file
    individually — see ``_remove_relay_file`` calls in the loop body.
    """

    for rel in (
        slice_grill_question_path(slice_name),
        slice_grill_answer_path(slice_name),
    ):
        _remove_relay_file(workspace_root, rel, slice_name)


def _read_slice_decision(workspace_root: str, slice_name: str) -> dict | None:
    """Read ``slices/<name>/decision.json`` for the recursion-bound check."""

    payload = read_gate_file(
        workspace_root,
        slice_decision_path(slice_name),
        schema_version="1",
    )
    if isinstance(payload, dict):
        return payload
    return None


def _read_slice_grill_question(workspace_root: str, slice_name: str) -> dict | None:
    payload = read_gate_file(
        workspace_root,
        slice_grill_question_path(slice_name),
        schema_version="1",
    )
    if isinstance(payload, dict):
        return payload
    return None


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


async def _run_one_slice_with_relay(
    *,
    parent_task: Any,
    slice_spec: dict[str, Any],
    workspace_root: str,
) -> SliceResult:
    """Run one slice through its sub-architect, handling parent grills.

    Loop:
      1. Invoke the slice runner.
      2. If it returns ``paused_for_grill`` — read the question, relay
         to the parent, then re-invoke (capped at
         ``MAX_GRILL_ROUNDS_PER_SLICE``).
      3. After every invocation, check ``slices/<name>/decision.json``
         for a nested ``spawn_sub_architects`` decision — that's the
         1-level recursion bound and the slice fails immediately.
      4. On ``completed`` — return success. Anything else (including
         exhausting the grill cap) returns failure.
    """

    slice_name = slice_spec.get("name") or ""
    slice_root = _ensure_slice_dir(workspace_root, slice_name)

    # Drop any stale relay files from a prior parent task run before the
    # slice even starts. Inside the loop the dispatcher manages each
    # file individually so a fresh parent answer is never wiped.
    _clear_stale_relay_files(workspace_root, slice_name)

    rounds_with_grill = 0

    while True:
        outcome = await _run_sub_architect_slice(
            parent_task=parent_task,
            slice_spec=slice_spec,
            workspace_root=workspace_root,
            slice_root=slice_root,
        )

        # 1-level recursion bound: a sub-architect that wrote a slice-scoped
        # decision.json with spawn_sub_architects must be rejected before
        # we let it proceed.
        nested = _read_slice_decision(workspace_root, slice_name)
        if nested is not None:
            nested_ok, nested_reason = validate_decision_for_role(
                decision=nested,
                is_sub_architect=True,
            )
            if not nested_ok:
                log.warning(
                    "trio.sub_architect.nested_spawn_rejected",
                    slice=slice_name,
                    reason=nested_reason,
                )
                return SliceResult(
                    name=slice_name,
                    status="failed",
                    reason=nested_reason or "nested spawn rejected (1-level bound)",
                )

        status = (outcome or {}).get("status")
        if status == "completed":
            return SliceResult(name=slice_name, status="completed")

        if status == "paused_for_grill":
            rounds_with_grill += 1
            if rounds_with_grill > MAX_GRILL_ROUNDS_PER_SLICE:
                return SliceResult(
                    name=slice_name,
                    status="failed",
                    reason=(
                        f"sub-architect exceeded {MAX_GRILL_ROUNDS_PER_SLICE} "
                        f"grill rounds without completing — runaway clarification "
                        f"loop; escalating"
                    ),
                )

            question_payload = _read_slice_grill_question(workspace_root, slice_name)
            question = ""
            if isinstance(question_payload, dict):
                q = question_payload.get("question")
                if isinstance(q, str):
                    question = q

            log.info(
                "trio.sub_architect.parent_relay",
                slice=slice_name,
                round=rounds_with_grill,
                question_preview=question[:120],
            )

            # Parent answers via its persisted session. The hook is
            # expected to write slices/<name>/grill_answer.json before
            # returning — that file is what the re-invoked sub-architect
            # reads on its next round.
            await _ask_parent_to_answer_grill(
                parent_task=parent_task,
                slice_name=slice_name,
                question=question,
                workspace_root=workspace_root,
            )

            # Remove just the question file before re-invoking — the
            # parent's freshly-written answer must survive so the
            # sub-architect can read it. (Wiping it here was the bug
            # that bit the first run of the test.)
            _remove_relay_file(
                workspace_root,
                slice_grill_question_path(slice_name),
                slice_name,
            )

            # Loop back — the runner reads the answer on its next round.
            continue

        # status == "failed" or unknown — terminal.
        reason = (outcome or {}).get("reason") or f"unknown sub-architect status: {status!r}"
        return SliceResult(name=slice_name, status="failed", reason=str(reason))


async def dispatch_sub_architects(
    *,
    parent_task: Any,
    workspace_root: str,
    slices: list[dict[str, Any]],
) -> SubArchitectDispatchResult:
    """Run sub-architects serially for every slice.

    Each slice runs through ``_run_one_slice_with_relay`` which composes
    the per-slice runtime and the parent-grill relay. On the first
    permanent failure the dispatcher short-circuits — subsequent
    slices do not run. This matches the parent-task lifecycle: a
    failed slice means the whole task can't ship; running the rest
    just wastes API spend.

    Returns :class:`SubArchitectDispatchResult` — the trio orchestrator
    reads ``.ok`` to decide between transitioning the parent task to
    ``FINAL_REVIEW`` (success) or ``BLOCKED`` (failure).
    """

    if not isinstance(slices, list) or not slices:
        return SubArchitectDispatchResult(
            ok=False,
            blocked_reason="dispatch_sub_architects called with empty slice list",
        )

    results: list[SliceResult] = []

    for slice_spec in slices:
        slice_result = await _run_one_slice_with_relay(
            parent_task=parent_task,
            slice_spec=slice_spec,
            workspace_root=workspace_root,
        )
        results.append(slice_result)
        if slice_result.status == "failed":
            return SubArchitectDispatchResult(
                ok=False,
                slice_results=results,
                blocked_reason=(
                    f"slice {slice_result.name!r}: {slice_result.reason or 'permanent failure'}"
                ),
            )

    return SubArchitectDispatchResult(ok=True, slice_results=results)


__all__ = [
    "MAX_GRILL_ROUNDS_PER_SLICE",
    "SliceResult",
    "SubArchitectDispatchResult",
    "dispatch_sub_architects",
    "validate_decision_for_role",
]
