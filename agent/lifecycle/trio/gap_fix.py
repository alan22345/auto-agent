"""Architect gap-fix loop — ADR-015 §4 / Phase 7.

When the final reviewer returns ``gaps_found``, the orchestrator
**resumes** the architect's persisted Session (Phase 6 stored it on
``ArchitectAttempt.session_blob_path``) and asks it to close the gaps.
The architect's reply is a fresh ``decision.json`` via the
``submit-architect-decision`` skill — typically
``{"action": "dispatch_new", "payload": {"items": [...]}}`` — and the
orchestrator dispatches the new items through the normal builder →
heavy-review loop.

Bounds (ADR-015 §4): **3 gap-fix rounds**. A 4th round skips the agent
entirely and returns a ``blocked`` decision so the caller can park the
task (non-freeform) or hand off to the improvement-agent standin
(freeform — Phase 10 wires the standin).
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from sqlalchemy import select

from agent.lifecycle.trio.architect import (
    _load_parent_for_run,
    _prepare_parent_workspace,
    create_architect_agent,
)
from agent.lifecycle.workspace_paths import DECISION_PATH
from agent.lifecycle.workspace_reader import read_gate_file
from shared.database import async_session
from shared.models import ArchitectAttempt

log = structlog.get_logger()


# Maximum gap-fix rounds before BLOCKED — ADR-015 §4.
MAX_GAP_FIX_ROUNDS = 3


async def _load_architect_session(parent_task_id: int):
    """Load the architect's most-recent persisted Session for this parent.

    Walks ``ArchitectAttempt`` rows newest-first for the parent and
    returns the first ``Session`` whose blob exists on disk. Returns
    ``None`` if no resumable session is available (caller falls back to a
    fresh architect run).
    """

    from agent.session import Session

    async with async_session() as s:
        rows = (
            (
                await s.execute(
                    select(ArchitectAttempt)
                    .where(ArchitectAttempt.task_id == parent_task_id)
                    .where(ArchitectAttempt.session_blob_path.is_not(None))
                    .order_by(ArchitectAttempt.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        return None

    # ArchitectAttempt.session_blob_path is workspace-relative
    # (Phase 6 saves it as ``trio-<id>.json``). The actual ``Session``
    # load happens with the workspace path available, inside
    # ``run_gap_fix`` — we just signal "resumable session exists" here.
    session_id = f"trio-{parent_task_id}"
    return Session(session_id=session_id, storage_dir="<placeholder>")


# Title connectives that almost always indicate a multi-subsystem item
# stitched into one ("Wire X + Y + Z"). Kept as lowercase word boundaries.
_OVERSIZED_TITLE_CONNECTIVES = (" + ", " and ", " with ", " plus ")

# Token thresholds for the size heuristic. Calibrated from task 28's
# G3 ("Wire CounterfactualSession + REST endpoints + WebSocket sibling
# streaming") which had 3 connectives, ~6 file paths across 4 layers,
# and stalled the coder for 30+ minutes in one item.
_MAX_FILE_PATHS_IN_DESCRIPTION = 4
_MAX_CONNECTIVES_IN_TITLE = 1


def _file_path_hits(text: str) -> int:
    """Count tokens that look like concrete file paths (foo/bar.py,
    src/x/y.ts, etc.). Cheap heuristic — false positives are fine; this
    is a warning signal, not a gate."""
    import re as _re

    return len(_re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|sql|yml|yaml|md|go|rs)", text or ""))


def _validate_item_size(item: dict) -> str | None:
    """Return a one-line oversize reason, or ``None`` if the item passes.

    Heuristic — see ``_GAP_FIX_PROMPT``'s sizing rule. Soft check: we
    warn and surface in attempt logs but don't block dispatch, because
    re-rolling the architect is expensive and the validator can have
    false positives. The warning shows up in the parent's
    ArchitectAttempt row and in the UI backlog viewer.
    """
    title = (item.get("title") or "").lower()
    description = item.get("description") or ""

    connectives = sum(title.count(c) for c in _OVERSIZED_TITLE_CONNECTIVES)
    if connectives > _MAX_CONNECTIVES_IN_TITLE:
        return (
            f"title stitches {connectives + 1} subsystems "
            f"({_OVERSIZED_TITLE_CONNECTIVES!r}); should be split"
        )

    file_paths = _file_path_hits(description)
    if file_paths > _MAX_FILE_PATHS_IN_DESCRIPTION:
        return (
            f"description names {file_paths} file paths "
            f"(>{_MAX_FILE_PATHS_IN_DESCRIPTION}); likely spans multiple subsystems"
        )

    return None


def validate_backlog_items(items: list[dict]) -> list[dict]:
    """Run the size heuristic over a list of dispatch_new items.

    Returns a list of ``{"id": str, "title": str, "reason": str}`` for
    every item that looks oversized. Empty list = all-clean. Called by
    ``run_gap_fix`` and surfaced in the dispatch log + decision payload
    so the UI can flag the items for human review.
    """
    warnings: list[dict] = []
    for it in items or []:
        reason = _validate_item_size(it)
        if reason:
            warnings.append(
                {
                    "id": str(it.get("id") or ""),
                    "title": str(it.get("title") or ""),
                    "reason": reason,
                }
            )
    return warnings


def _render_gaps(gaps: list[dict]) -> str:
    if not gaps:
        return "(no gaps)"
    lines: list[str] = []
    for g in gaps:
        lines.append(f"- {g.get('description', '')} (routes: {g.get('affected_routes') or []!r})")
    return "\n".join(lines)


_GAP_FIX_PROMPT = """\
The final reviewer found the following gaps after the per-item loop
drained. Your design.md, backlog.json and current decision.json are
pinned in this system prompt — re-read them and decide how to close
the gaps below.

You MUST use the ``submit-architect-decision`` skill to write
``.auto-agent/decision.json``. The preferred action is
``"dispatch_new"`` with new backlog items in the payload. If you
believe the gaps can't be closed without escalation, use
``"escalate"`` instead.

This is gap-fix round {round_idx} of {max_rounds}. After {max_rounds}
rounds the orchestrator blocks the task automatically.

== Hard rule ==
If a gap says "domain X was never built" or "module Y is absent",
the dispatch_new items MUST be the work to BUILD that module (create
files, schemas, migrations, routes, services, tests). Do NOT emit
"fix references in other domains" / "rename methods" / "verify
upstream signatures" — that pattern produced the gap in the first
place (the per-item loop drained a backlog of preparatory pin-tests
without ever building the target domain). Read ``.auto-agent/design.md``
for the canonical module layout and emit one item per file or per
tight group of files.

== Sizing rule (mandatory) ==
**Never defer; split aggressively.** Each item must be implementable
by ONE coder turn in a healthy context window — that means
approximately one cohesive subsystem (e.g. one set of related files
in a single layer: models OR routes OR a single React component +
its hook, NOT all three together).

Forbidden item shapes:
- Titles containing "+", "and", "with" stitching multiple subsystems
  (e.g. "Wire X + REST endpoints + WebSocket streaming + tests").
- Descriptions naming files across 3+ distinct layers (model + route
  + UI + e2e test in one item).
- Items that defer work to "later", "phase N", "v2", or "follow-up".

If a gap is genuinely too large to close in one cohesive item, split
it into 3-6 smaller items in this same ``dispatch_new`` (e.g. one
item for the model + repo, one for REST routes, one for the
WebSocket layer, one for the UI shell, one for integration tests).
Prefer 5 small items over 1 fat item — the per-item reviewer and
smoke gate work much better on focused diffs.

== Item contract ==
Every dispatch_new item MUST include three fields:
- ``id``: a unique handle (e.g. ``G1``, ``G2``, ... — the orchestrator
  will auto-assign one if you omit it, but explicit IDs make logs and
  the per-item tiebreak prompts traceable)
- ``title``: one-line imperative summary, **single cohesive subsystem**
  (no "+"-stitched scopes — split instead)
- ``description``: 1-3 sentences naming the SPECIFIC files to create
  or modify (e.g. ``src/harpoon/funnel/repositories.py``). Concrete
  file paths matter: the orchestrator verifies the named paths exist
  before accepting an item as done. An item whose description names
  no concrete file paths is at high risk of being marked done without
  any real work having happened. **If you find yourself listing more
  than ~4 file paths spanning 3+ subsystems, split into multiple
  items instead.**

== Final reviewer's gaps ==
{gaps}
"""


async def run_gap_fix(
    *,
    parent_task_id: int,
    gaps: list[dict[str, Any]],
    round_idx: int,
) -> dict[str, Any]:
    """Resume the architect with the gap list; return its decision.

    Bound check fires first — round_idx > MAX_GAP_FIX_ROUNDS returns
    ``{"action": "blocked", "reason": "gap_fix_round_limit"}`` without
    invoking any agent.

    Returns the decision dict read from ``.auto-agent/decision.json``.
    On dispatch_new the decision's ``items`` (lifted from
    ``payload.items``) are returned in a top-level ``items`` key for
    caller convenience.
    """

    if round_idx > MAX_GAP_FIX_ROUNDS:
        log.info(
            "trio.gap_fix.round_limit_reached",
            parent_id=parent_task_id,
            round_idx=round_idx,
        )
        return {
            "action": "blocked",
            "reason": "gap_fix_round_limit",
            "rounds_exhausted": MAX_GAP_FIX_ROUNDS,
        }

    fields = await _load_parent_for_run(parent_task_id)
    workspace = await _prepare_parent_workspace(fields.get("__parent"))
    workspace_root = workspace.root if hasattr(workspace, "root") else str(workspace)

    # The architect ran originally without a --session-id (claude_cli
    # provider generates its own UUID), so we cannot --resume it from the
    # auto-agent Session blob — claude CLI doesn't know that UUID. Run
    # fresh: the checkpoint system prompt pins design.md + backlog.json +
    # decision.json so the architect has full load-bearing context, and
    # the gap list comes in via the prompt. Even with the blob present,
    # the harpoon #25 + #28 incidents showed resume is unreliable across
    # workspace recreates / stashes — fresh-run is the safer default.
    agent = create_architect_agent(
        workspace=workspace,
        task_id=parent_task_id,
        task_description=fields["task_description"],
        phase="checkpoint",
        repo_name=fields["repo_name"],
        home_dir=fields["home_dir"],
        org_id=fields["org_id"],
        session=None,
    )

    prompt = _GAP_FIX_PROMPT.format(
        round_idx=round_idx,
        max_rounds=MAX_GAP_FIX_ROUNDS,
        gaps=_render_gaps(gaps),
    )

    # Clear stale decision.json so we don't pick up a prior round's verdict.
    decision_abs = os.path.join(workspace_root, DECISION_PATH)
    if os.path.isfile(decision_abs):
        os.remove(decision_abs)

    await agent.run(prompt, resume=False)

    payload = read_gate_file(workspace_root, DECISION_PATH, schema_version="1")
    if not isinstance(payload, dict):
        log.warning(
            "trio.gap_fix.missing_decision",
            parent_id=parent_task_id,
            round_idx=round_idx,
        )
        return {
            "action": "blocked",
            "reason": "architect did not write decision.json on gap-fix turn",
        }

    decision = dict(payload)
    inner_items = (
        decision.get("payload", {}).get("items")
        if isinstance(decision.get("payload"), dict)
        else None
    )
    if isinstance(inner_items, list):
        decision["items"] = list(inner_items)

    size_warnings = validate_backlog_items(decision.get("items") or [])
    if size_warnings:
        decision["size_warnings"] = size_warnings
        log.warning(
            "trio.gap_fix.oversized_items",
            parent_id=parent_task_id,
            round_idx=round_idx,
            warnings=size_warnings,
        )

    log.info(
        "trio.gap_fix.decision",
        parent_id=parent_task_id,
        round_idx=round_idx,
        action=decision.get("action"),
        item_count=len(decision.get("items") or []),
        oversized_count=len(size_warnings),
    )
    return decision


__all__ = [
    "MAX_GAP_FIX_ROUNDS",
    "run_gap_fix",
    "validate_backlog_items",
]
