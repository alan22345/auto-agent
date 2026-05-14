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
