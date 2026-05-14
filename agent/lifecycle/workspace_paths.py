"""Workspace-relative path constants for the skills-bridge — ADR-015 §12.

Every skill writes to a known path under ``.auto-agent/`` at the repo
root; the orchestrator reads the file after ``agent.run`` returns. This
module is the single source of truth for those paths so neither the skill
markdown files nor the orchestrator builds them inline.

All paths returned by this module are **relative**; the orchestrator
joins them with the workspace root.
"""

from __future__ import annotations

AUTO_AGENT_DIR = ".auto-agent"

# ---------------------------------------------------------------------------
# Top-level gate files (one per skill that targets a fixed path).
# ---------------------------------------------------------------------------

GRILL_PATH = f"{AUTO_AGENT_DIR}/grill.json"
PLAN_PATH = f"{AUTO_AGENT_DIR}/plan.md"
PLAN_APPROVAL_PATH = f"{AUTO_AGENT_DIR}/plan_approval.json"
DESIGN_PATH = f"{AUTO_AGENT_DIR}/design.md"
BACKLOG_PATH = f"{AUTO_AGENT_DIR}/backlog.json"
DECISION_PATH = f"{AUTO_AGENT_DIR}/decision.json"
FINAL_REVIEW_PATH = f"{AUTO_AGENT_DIR}/final_review.json"
PR_REVIEW_PATH = f"{AUTO_AGENT_DIR}/pr_review.json"
SMOKE_RESULT_PATH = f"{AUTO_AGENT_DIR}/smoke_result.json"
ARCHITECT_LOG_PATH = f"{AUTO_AGENT_DIR}/architect_log.md"


# ---------------------------------------------------------------------------
# Design-doc header (Phase 7.6) — stamps every design.md with its task id so
# the gate ignores leftover artefacts from previous tasks that reused the same
# workspace path. The header is a markdown HTML comment so the rendered design
# stays clean.
# ---------------------------------------------------------------------------

DESIGN_HEADER_FMT = "<!-- auto-agent: task_id={task_id} -->"


def format_design_header(task_id: int) -> str:
    return DESIGN_HEADER_FMT.format(task_id=task_id)


def strip_design_header(design_md: str) -> str:
    """Remove the design-id header (and the blank line that follows) if
    present. Returns the markdown unchanged when no header is detected.
    """
    if not design_md:
        return design_md
    first_line, _sep, rest = design_md.partition("\n")
    stripped = first_line.strip()
    if not (stripped.startswith("<!-- auto-agent: task_id=") and stripped.endswith("-->")):
        return design_md
    # Drop a single trailing blank line if it was written by ``write_design``.
    if rest.startswith("\n"):
        rest = rest[1:]
    return rest


# ---------------------------------------------------------------------------
# Templated paths — one entry per concrete invocation.
# ---------------------------------------------------------------------------


def review_path(item_id: str) -> str:
    """Path to the per-item heavy-reviewer verdict — §3."""

    return f"{AUTO_AGENT_DIR}/reviews/{item_id}.json"


def decision_history_path(seq: int) -> str:
    """Path to the architect's per-decision rationale snapshot — §13."""

    return f"{AUTO_AGENT_DIR}/decisions/{seq}.json"


def slice_dir(name: str) -> str:
    """Per-sub-architect namespace — §9."""

    return f"{AUTO_AGENT_DIR}/slices/{name}"


def slice_design_path(name: str) -> str:
    return f"{slice_dir(name)}/design.md"


def slice_backlog_path(name: str) -> str:
    return f"{slice_dir(name)}/backlog.json"


def slice_grill_question_path(name: str) -> str:
    """Sub-architect → parent grill question relay — §10."""

    return f"{slice_dir(name)}/grill_question.json"


def slice_grill_answer_path(name: str) -> str:
    """Parent → sub-architect grill answer relay — §10."""

    return f"{slice_dir(name)}/grill_answer.json"


def slice_decision_path(name: str) -> str:
    """Sub-architect's per-cycle decision file — mirrors top-level
    ``decision.json`` but namespaced under the slice. Used by Phase 8's
    1-level recursion bound check (sub-architects writing
    ``spawn_sub_architects`` here is rejected).
    """

    return f"{slice_dir(name)}/decision.json"


def slice_reviews_dir(name: str) -> str:
    """Directory for per-item heavy-reviewer verdicts inside a slice.

    Mirrors the top-level ``.auto-agent/reviews/`` layout but namespaced
    so two slices can have an item with the same id without colliding.
    """

    return f"{slice_dir(name)}/reviews"


def slice_review_path(name: str, item_id: str) -> str:
    """Path to one item's heavy-review verdict under a slice."""

    return f"{slice_reviews_dir(name)}/{item_id}.json"
