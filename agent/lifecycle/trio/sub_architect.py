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

import json
import os
from dataclasses import dataclass, field
from typing import Any

import structlog

from agent.lifecycle.trio.architect import create_architect_agent
from agent.lifecycle.trio.dispatcher import dispatch_item
from agent.lifecycle.trio.final_reviewer import run_final_review
from agent.lifecycle.trio.validators import validate_backlog
from agent.lifecycle.workspace_paths import (
    slice_backlog_path,
    slice_decision_path,
    slice_design_path,
    slice_dir,
    slice_grill_answer_path,
    slice_grill_question_path,
)
from agent.lifecycle.workspace_reader import read_gate_file
from agent.session import Session

log = structlog.get_logger()


# Maximum number of backlog-emit attempts before a slice permanently fails
# (mirrors the parent architect's 3-attempt structural-validation bound).
MAX_BACKLOG_ATTEMPTS_PER_SLICE = 3


class MissingGrillAnswerError(RuntimeError):
    """Raised when the parent architect fails to write a grill answer file.

    After ``_ask_parent_to_answer_grill`` resumes the parent's session
    with a retry, the orchestrator gives up and signals the dispatcher
    so the slice can be parked / escalated rather than spinning forever.
    """


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
) -> dict[str, Any]:
    """Run one slice's full design → backlog → builder/review cycle.

    Returns a dict with at minimum ``status`` ∈ {``"completed"``,
    ``"paused_for_grill"``, ``"failed"``}. ``"paused_for_grill"``
    indicates a grill question was written and the dispatcher should
    relay it to the parent before re-invoking this runner.

    Production runtime (ADR-015 §9 / §13):

      1. Design pass — sub-architect agent writes ``slices/<name>/design.md``.
         No design-approval gate runs — the parent's design.md is the
         single approval artefact for the entire run; the slice design is
         internal documentation.
      2. Backlog emit — sub-architect writes ``slices/<name>/backlog.json``.
         Structural validation runs against the slice backlog with the
         same ``validate_backlog`` rules and the same 3-attempt bound
         that the parent architect uses.
      3. Per-item builder→heavy-review loop — every backlog item flows
         through :func:`agent.lifecycle.trio.dispatcher.dispatch_item` with
         ``slice_name`` set so verdicts land under the slice namespace.
      4. Final review — slice-scoped final reviewer runs on the slice's
         affected_routes. ``gaps_found`` triggers a bounded gap-fix loop
         (Phase 7 mechanism, slice-scoped) before the slice fails.

    Recursion bound: every architect run inside this function passes
    ``slice_name`` to ``create_architect_agent`` so the agent reads its
    own slice's pinned context, and any ``spawn_sub_architects`` decision
    a sub-architect emits is rejected by :func:`validate_decision_for_role`.

    On any grill request mid-design — the sub-architect writes
    ``slices/<name>/grill_question.json`` and stops; the function returns
    ``status="paused_for_grill"`` so the dispatcher relays to the parent.
    """

    slice_name = str(slice_spec.get("name") or "")
    if not slice_name:
        return {
            "status": "failed",
            "reason": "slice spec is missing a 'name' field",
        }

    scope = str(slice_spec.get("scope") or "")
    parent_task_id = int(getattr(parent_task, "id", 0) or 0)
    repo = getattr(parent_task, "repo", None)
    repo_name = repo.name if repo is not None else None
    org_id = getattr(parent_task, "organization_id", None)

    # Ensure the slice's namespace exists on disk before any skill writes.
    os.makedirs(slice_root, exist_ok=True)

    # 1. Design pass --------------------------------------------------------
    design_outcome = await _run_slice_design(
        workspace_root=workspace_root,
        slice_name=slice_name,
        scope=scope,
        parent_task_id=parent_task_id,
        repo_name=repo_name,
        org_id=org_id,
    )
    if design_outcome["status"] != "completed":
        return design_outcome

    # 2. Backlog emit + validator (bounded retry) ---------------------------
    backlog_outcome = await _run_slice_backlog_emit(
        workspace_root=workspace_root,
        slice_name=slice_name,
        scope=scope,
        parent_task_id=parent_task_id,
        repo_name=repo_name,
        org_id=org_id,
    )
    if backlog_outcome["status"] != "completed":
        return backlog_outcome
    items: list[dict[str, Any]] = backlog_outcome["items"]

    # 3. Per-item builder→heavy-review loop ---------------------------------
    per_item_outcome = await _run_slice_per_item_loop(
        workspace_root=workspace_root,
        slice_name=slice_name,
        parent_task_id=parent_task_id,
        items=items,
        repo_name=repo_name,
        org_id=org_id,
    )
    if per_item_outcome["status"] != "completed":
        return per_item_outcome

    # 4. Slice-scoped final reviewer ----------------------------------------
    final_outcome = await _run_slice_final_review(
        workspace_root=workspace_root,
        slice_name=slice_name,
        parent_task_id=parent_task_id,
        items=items,
        repo_name=repo_name,
        org_id=org_id,
    )
    return final_outcome


# ---------------------------------------------------------------------------
# Per-slice runtime helpers — each step is a top-level coroutine so tests
# can patch them individually (or patch the LLM seam they all share via
# ``create_architect_agent``).
# ---------------------------------------------------------------------------


def _slice_session_id(parent_task_id: int, slice_name: str) -> str:
    """Stable session id for a slice — lets resume work across grill rounds.

    The session blob lives under the workspace root so the parent's
    autoritative ``trio-<parent_id>.json`` is never overwritten by a
    sub-architect run.
    """

    return f"trio-{parent_task_id}-slice-{slice_name}"


def _build_slice_session(workspace_root: str, parent_task_id: int, slice_name: str):
    """Return a :class:`agent.session.Session` rooted in the workspace."""

    return Session(
        session_id=_slice_session_id(parent_task_id, slice_name),
        storage_dir=workspace_root,
    )


def _result_output(result: Any) -> str:
    """Extract text output from whatever ``agent.run`` returned."""

    if hasattr(result, "output"):
        return result.output or ""
    return str(result) if result is not None else ""


async def _run_slice_design(
    *,
    workspace_root: str,
    slice_name: str,
    scope: str,
    parent_task_id: int,
    repo_name: str | None,
    org_id: int | None,
) -> dict[str, Any]:
    """Drive the sub-architect's design turn against ``slices/<name>/design.md``.

    The sub-architect uses the same ``submit-design`` skill the parent
    architect uses, but the agent's pinned context is slice-scoped via
    ``create_architect_agent(slice_name=...)`` so the prompt instructs
    the model to write under the slice namespace. ADR-015 §2 makes the
    parent's design.md the single approval artefact for the run, so no
    plan_approval gate fires for slice designs.

    Returns ``{"status": "completed"}`` on success, ``"paused_for_grill"``
    when the model wrote a grill question instead, or ``"failed"`` when
    the design file is missing after the retry.
    """

    session = _build_slice_session(workspace_root, parent_task_id, slice_name)
    # If we're re-entering after a parent grill answer landed, the
    # slice's session blob from the previous turn exists on disk —
    # resume from it so the model sees its own prior context. First
    # entry has no blob; resume is False.
    prior_session_existed = await session.load() is not None

    design_rel = slice_design_path(slice_name)
    question_rel = slice_grill_question_path(slice_name)
    answer_rel = slice_grill_answer_path(slice_name)

    agent = create_architect_agent(
        workspace=workspace_root,
        task_id=parent_task_id,
        task_description=f"Sub-architect slice '{slice_name}': {scope}",
        phase="design",
        repo_name=repo_name,
        home_dir=None,
        org_id=org_id,
        session=session,
        slice_name=slice_name,
    )

    answer_text = ""
    if prior_session_existed and os.path.isfile(os.path.join(workspace_root, answer_rel)):
        try:
            with open(os.path.join(workspace_root, answer_rel)) as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                answer_text = str(payload.get("answer", ""))
        except (OSError, json.JSONDecodeError):
            answer_text = ""

    if prior_session_existed and answer_text:
        prompt = (
            f"The parent architect answered your previous grill question:\n\n"
            f"{answer_text}\n\n"
            f"Now resume your slice design and write `{design_rel}` via the\n"
            f"`submit-design` skill."
        )
    else:
        prompt = (
            f"You are the sub-architect for slice '{slice_name}'.\n\n"
            f"Slice scope:\n{scope}\n\n"
            f"Write the slice's design doc via the `submit-design` skill. The\n"
            f"file path is `{design_rel}` (NOT the root `.auto-agent/design.md`).\n"
            f"The parent architect's design.md is the single approval artefact\n"
            f"for the run; your slice design is internal documentation. After\n"
            f"writing, stop.\n\n"
            f"If you genuinely need a design clarification only the parent\n"
            f"architect can answer, use the `submit-grill-question` skill to\n"
            f"write `{question_rel}` and stop — the orchestrator will relay\n"
            f"to the parent and resume you with the answer."
        )

    await agent.run(prompt, resume=prior_session_existed)
    await session.save(agent.messages, agent.api_messages)

    if os.path.isfile(os.path.join(workspace_root, question_rel)):
        return {"status": "paused_for_grill"}

    if not os.path.isfile(os.path.join(workspace_root, design_rel)):
        # Retry once with an amended prompt — ADR-015 §12 missing-output retry.
        retry_prompt = (
            "Your previous response did not write the slice design. You MUST\n"
            f"call the `submit-design` skill (target `{design_rel}`) before\n"
            "stopping. Do that now."
        )
        await agent.run(retry_prompt, resume=True)
        await session.save(agent.messages, agent.api_messages)

        if os.path.isfile(os.path.join(workspace_root, question_rel)):
            return {"status": "paused_for_grill"}
        if not os.path.isfile(os.path.join(workspace_root, design_rel)):
            return {
                "status": "failed",
                "reason": (
                    f"sub-architect '{slice_name}' did not write {design_rel} after one retry"
                ),
            }

    return {"status": "completed"}


def _format_backlog_rejections(rejections: list) -> str:
    """Render a validator failure list back into the architect's prompt."""

    lines: list[str] = []
    for r in rejections:
        idx = getattr(r, "item_index", -1)
        fld = getattr(r, "field", "?")
        reason = getattr(r, "reason", "?")
        lines.append(f"- item[{idx}].{fld}: {reason}")
    return "\n".join(lines)


async def _run_slice_backlog_emit(
    *,
    workspace_root: str,
    slice_name: str,
    scope: str,
    parent_task_id: int,
    repo_name: str | None,
    org_id: int | None,
) -> dict[str, Any]:
    """Drive the sub-architect's backlog turn against ``slices/<name>/backlog.json``.

    Loops up to :data:`MAX_BACKLOG_ATTEMPTS_PER_SLICE` times — the
    structural validator's rejections are fed back into the agent's next
    turn so it can fix items one-by-one without restarting the design.
    """

    session = _build_slice_session(workspace_root, parent_task_id, slice_name)
    backlog_rel = slice_backlog_path(slice_name)
    question_rel = slice_grill_question_path(slice_name)

    base_prompt = (
        f"You are the sub-architect for slice '{slice_name}', resuming after\n"
        f"your design doc was written. Emit the structured backlog via the\n"
        f"`submit-backlog` skill — target file `{backlog_rel}` (NOT the\n"
        f"root `.auto-agent/backlog.json`). Each item must satisfy the same\n"
        f"validator the parent backlog uses: title, ≥80-word description,\n"
        f"justification, affected_routes (list), affected_files_estimate.\n"
        f"Forbidden no-defer phrases (Phase 1/v2/TODO(phase/etc.) fail the\n"
        f"slice. Slice scope reminder:\n\n{scope}"
    )

    last_rejection_summary = ""

    for attempt in range(MAX_BACKLOG_ATTEMPTS_PER_SLICE):
        agent = create_architect_agent(
            workspace=workspace_root,
            task_id=parent_task_id,
            task_description=f"Sub-architect slice '{slice_name}': {scope}",
            phase="backlog_emit",
            repo_name=repo_name,
            home_dir=None,
            org_id=org_id,
            session=session,
            slice_name=slice_name,
        )

        if attempt == 0:
            prompt = base_prompt
            resume = True  # resume from the design turn's session
        else:
            prompt = (
                f"Your previous backlog emit failed the structural validator.\n"
                f"Fix every rejection below and re-emit via `submit-backlog`:\n\n"
                f"{last_rejection_summary}\n\n"
                f"Target path remains `{backlog_rel}`."
            )
            resume = True

        await agent.run(prompt, resume=resume)
        await session.save(agent.messages, agent.api_messages)

        if os.path.isfile(os.path.join(workspace_root, question_rel)):
            return {"status": "paused_for_grill"}

        backlog_payload = read_gate_file(
            workspace_root,
            backlog_rel,
            schema_version="1",
        )
        items: list[dict[str, Any]] | None = None
        if isinstance(backlog_payload, dict):
            raw_items = backlog_payload.get("items")
            if isinstance(raw_items, list):
                items = [i for i in raw_items if isinstance(i, dict)]

        if items is None:
            last_rejection_summary = (
                f"backlog file `{backlog_rel}` is missing or not a JSON\n"
                "object with an `items` list."
            )
            continue

        validation = validate_backlog(items)
        if validation.ok:
            return {"status": "completed", "items": items}

        last_rejection_summary = _format_backlog_rejections(validation.rejections)
        log.info(
            "trio.sub_architect.backlog_rejected",
            slice=slice_name,
            attempt=attempt + 1,
            rejections=len(validation.rejections),
        )

    return {
        "status": "failed",
        "reason": (
            f"sub-architect '{slice_name}' could not emit a valid backlog "
            f"in {MAX_BACKLOG_ATTEMPTS_PER_SLICE} attempts; last rejection: "
            f"{last_rejection_summary[:300]}"
        ),
    }


async def _run_slice_per_item_loop(
    *,
    workspace_root: str,
    slice_name: str,
    parent_task_id: int,
    items: list[dict[str, Any]],
    repo_name: str | None,
    org_id: int | None,
) -> dict[str, Any]:
    """Walk the slice backlog through the parent dispatcher with slice_name.

    Reuses :func:`agent.lifecycle.trio.dispatcher.dispatch_item` and
    threads ``slice_name`` so the heavy-reviewer writes verdicts under
    ``slices/<name>/reviews/<id>.json``. The first permanent failure (an
    item that exhausts MAX_ROUNDS without tiebreak success) fails the
    slice.
    """

    for item in items:
        result = await dispatch_item(
            parent_task_id=parent_task_id,
            work_item=item,
            workspace=workspace_root,
            repo_name=repo_name,
            home_dir=None,
            org_id=org_id,
            slice_name=slice_name,
        )
        if not result.ok:
            reason = result.failure_reason or (
                "needs_tiebreak" if result.needs_tiebreak else "item failed"
            )
            return {
                "status": "failed",
                "reason": (
                    f"sub-architect '{slice_name}' item {item.get('id', '?')!r} failed: {reason}"
                ),
            }

    return {"status": "completed"}


async def _run_slice_final_review(
    *,
    workspace_root: str,
    slice_name: str,
    parent_task_id: int,
    items: list[dict[str, Any]],
    repo_name: str | None,
    org_id: int | None,
) -> dict[str, Any]:
    """Run the slice-scoped final reviewer.

    On ``passed`` → slice succeeds. On ``gaps_found`` we resume the slice
    architect's session for up to two gap-fix rounds; if gaps persist the
    slice fails. (We don't reach into the parent's gap-fix module —
    sub-architects own their own gap-fix loop, the same way the parent
    does, just scoped to the slice's pinned context.)
    """

    result = await run_final_review(
        workspace_root=workspace_root,
        parent_task_id=parent_task_id,
        grill_output="",
        base_branch="main",
        previous_gaps=None,
        previous_attempt_summary="",
        repo_name=repo_name,
        home_dir=None,
        org_id=org_id,
        slice_name=slice_name,
    )

    if result.verdict == "passed":
        return {"status": "completed"}

    # gaps_found — try one slice-scoped gap-fix round, then re-review.
    gap_fix_outcome = await _run_slice_gap_fix(
        workspace_root=workspace_root,
        slice_name=slice_name,
        parent_task_id=parent_task_id,
        gaps=result.gaps,
        repo_name=repo_name,
        org_id=org_id,
    )
    if gap_fix_outcome["status"] != "completed":
        return gap_fix_outcome

    # New items dispatched; re-run final review to confirm the gaps closed.
    rerun = await run_final_review(
        workspace_root=workspace_root,
        parent_task_id=parent_task_id,
        grill_output="",
        base_branch="main",
        previous_gaps=result.gaps,
        previous_attempt_summary="gap-fix round 1 ran new items",
        repo_name=repo_name,
        home_dir=None,
        org_id=org_id,
        slice_name=slice_name,
    )
    if rerun.verdict == "passed":
        return {"status": "completed"}

    return {
        "status": "failed",
        "reason": (
            f"sub-architect '{slice_name}' final review still has gaps after "
            f"one gap-fix round: {[g.get('description', '') for g in rerun.gaps][:3]}"
        ),
    }


async def _run_slice_gap_fix(
    *,
    workspace_root: str,
    slice_name: str,
    parent_task_id: int,
    gaps: list[dict[str, Any]],
    repo_name: str | None,
    org_id: int | None,
) -> dict[str, Any]:
    """Resume the slice architect's session and ask it to close gaps.

    The architect emits a fresh decision (``dispatch_new`` with new items)
    via the ``submit-architect-decision`` skill into
    ``slices/<name>/decision.json``. The new items run through the
    per-item loop with slice scoping, then control returns so the caller
    can re-run final review.
    """

    session = _build_slice_session(workspace_root, parent_task_id, slice_name)
    decision_rel = slice_decision_path(slice_name)

    # Clear stale decision so a previous round doesn't satisfy this turn.
    decision_abs = os.path.join(workspace_root, decision_rel)
    if os.path.isfile(decision_abs):
        os.remove(decision_abs)

    gap_lines = (
        "\n".join(
            f"- {g.get('description', '')} (routes: {g.get('affected_routes') or []!r})"
            for g in gaps
        )
        or "(no gap descriptions)"
    )

    prompt = (
        f"The slice final reviewer found gaps for slice '{slice_name}':\n\n"
        f"{gap_lines}\n\n"
        f"Resume your slice architect session and decide how to close these.\n"
        f"You MUST use `submit-architect-decision` to write `{decision_rel}`.\n"
        f'Prefer `action="dispatch_new"` with new backlog items in payload.\n'
        f'If the gaps cannot be closed at this layer, use `"escalate"`.'
    )

    agent = create_architect_agent(
        workspace=workspace_root,
        task_id=parent_task_id,
        task_description=f"Sub-architect slice '{slice_name}' gap fix",
        phase="checkpoint",
        repo_name=repo_name,
        home_dir=None,
        org_id=org_id,
        session=session,
        slice_name=slice_name,
    )
    await agent.run(prompt, resume=True)
    await session.save(agent.messages, agent.api_messages)

    decision = read_gate_file(workspace_root, decision_rel, schema_version="1")
    if not isinstance(decision, dict):
        return {
            "status": "failed",
            "reason": (f"sub-architect '{slice_name}' did not emit a gap-fix decision.json"),
        }

    # Recursion-bound check — a sub-architect cannot spawn deeper.
    ok, reason = validate_decision_for_role(
        decision=decision,
        is_sub_architect=True,
    )
    if not ok:
        return {
            "status": "failed",
            "reason": reason or "nested spawn rejected (1-level bound)",
        }

    action = decision.get("action")
    payload = decision.get("payload", {})
    if action == "dispatch_new":
        new_items_raw = payload.get("items") if isinstance(payload, dict) else None
        new_items: list[dict[str, Any]] = [i for i in (new_items_raw or []) if isinstance(i, dict)]
        if not new_items:
            return {
                "status": "failed",
                "reason": (f"sub-architect '{slice_name}' gap-fix dispatch_new produced no items"),
            }
        return await _run_slice_per_item_loop(
            workspace_root=workspace_root,
            slice_name=slice_name,
            parent_task_id=parent_task_id,
            items=new_items,
            repo_name=repo_name,
            org_id=org_id,
        )

    return {
        "status": "failed",
        "reason": (f"sub-architect '{slice_name}' gap-fix decision action {action!r}: {payload}"),
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
) -> None:
    """Resume the parent architect's session to answer a grill question.

    The contract: when this function returns successfully,
    ``slices/<name>/grill_answer.json`` is on disk under
    ``workspace_root``. Production wire-up (ADR-015 §10):

      1. Load the parent architect's persisted Session
         (``<workspace_root>/trio-<parent_id>.json``).
      2. Resume the AgentLoop with ``resume=True`` and a prompt that
         spells out the question and tells the architect to reply via
         the ``submit-grill-answer`` skill.
      3. Re-read ``slices/<name>/grill_answer.json``. If missing, retry
         once with an amended prompt (per ADR-015 §12 missing-output
         policy).
      4. Persist the parent's session again so the round-trip is
         recorded — symmetric with Phase 6's ``_persist_architect_session``.
      5. Return; the parent's task status remains ``AWAITING_SUB_ARCHITECTS``
         throughout — no state-machine transition fires from this seam.

    Raises :class:`MissingGrillAnswerError` after both retries miss; the
    dispatcher catches it and surfaces the slice failure.
    """

    parent_task_id = int(getattr(parent_task, "id", 0) or 0)
    repo = getattr(parent_task, "repo", None)
    repo_name = repo.name if repo is not None else None
    org_id = getattr(parent_task, "organization_id", None)
    description = (
        getattr(parent_task, "description", None) or getattr(parent_task, "title", "") or ""
    )

    answer_rel = slice_grill_answer_path(slice_name)
    answer_abs = os.path.join(workspace_root, answer_rel)
    if os.path.isfile(answer_abs):
        # Drop any stale answer from a previous round so we can detect
        # whether the parent actually wrote a fresh one this turn.
        os.remove(answer_abs)

    parent_session = Session(
        session_id=f"trio-{parent_task_id}",
        storage_dir=workspace_root,
    )
    loaded = await parent_session.load()
    if loaded is None:
        log.warning(
            "trio.sub_architect.parent_session_missing",
            parent_id=parent_task_id,
            slice=slice_name,
        )
        # Even without a prior session blob we still attempt the relay —
        # the parent has its design.md pinned in the system prompt and
        # can answer from that alone.

    base_prompt = (
        f"Sub-architect '{slice_name}' has a clarification question:\n\n"
        f"{question}\n\n"
        f"Answer this from your design context. Do NOT delegate to the user\n"
        f"— you have the full design and architect log. Reply by calling the\n"
        f"`submit-grill-answer` skill, which will write the answer to\n"
        f"`{answer_rel}`."
    )

    last_output = ""
    for attempt in range(2):
        agent = create_architect_agent(
            workspace=workspace_root,
            task_id=parent_task_id,
            task_description=description,
            phase="consult",
            repo_name=repo_name,
            home_dir=None,
            org_id=org_id,
            session=parent_session,
        )

        if attempt == 0:
            prompt = base_prompt
        else:
            prompt = (
                f"{base_prompt}\n\n"
                f"Your previous response did not write `{answer_rel}`. You\n"
                f"MUST call the `submit-grill-answer` skill before stopping."
            )

        result = await agent.run(prompt, resume=True)
        last_output = _result_output(result)
        await parent_session.save(agent.messages, agent.api_messages)

        if os.path.isfile(answer_abs):
            log.info(
                "trio.sub_architect.parent_answered_grill",
                parent_id=parent_task_id,
                slice=slice_name,
                attempt=attempt + 1,
            )
            return

    raise MissingGrillAnswerError(
        f"parent architect did not write {answer_rel} after 2 attempts. "
        f"Last output preview: {last_output[:200]!r}"
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
            try:
                await _ask_parent_to_answer_grill(
                    parent_task=parent_task,
                    slice_name=slice_name,
                    question=question,
                    workspace_root=workspace_root,
                )
            except MissingGrillAnswerError as exc:
                log.warning(
                    "trio.sub_architect.parent_grill_missing_answer",
                    slice=slice_name,
                    error=str(exc),
                )
                return SliceResult(
                    name=slice_name,
                    status="failed",
                    reason=(f"parent architect failed to answer grill question: {exc}"),
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
    "MAX_BACKLOG_ATTEMPTS_PER_SLICE",
    "MAX_GRILL_ROUNDS_PER_SLICE",
    "MissingGrillAnswerError",
    "SliceResult",
    "SubArchitectDispatchResult",
    "dispatch_sub_architects",
    "validate_decision_for_role",
]
