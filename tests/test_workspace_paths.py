"""Spec for ``agent.lifecycle.workspace_paths`` — ADR-015 §12.

The module is the single source of truth for every workspace-relative path
used by the skills-bridge. Helpers compose path templates so that the
orchestrator never builds them inline.

Everything returned is a *relative* path under ``.auto-agent/``; the
orchestrator joins with the workspace root.
"""

from __future__ import annotations

from agent.lifecycle import workspace_paths as wp


def test_auto_agent_dir_constant() -> None:
    assert wp.AUTO_AGENT_DIR == ".auto-agent"


def test_top_level_gate_paths() -> None:
    """Every top-level gate file is under ``.auto-agent/``."""

    assert wp.GRILL_PATH == ".auto-agent/grill.json"
    assert wp.PLAN_PATH == ".auto-agent/plan.md"
    assert wp.PLAN_APPROVAL_PATH == ".auto-agent/plan_approval.json"
    assert wp.DESIGN_PATH == ".auto-agent/design.md"
    assert wp.BACKLOG_PATH == ".auto-agent/backlog.json"
    assert wp.DECISION_PATH == ".auto-agent/decision.json"
    assert wp.FINAL_REVIEW_PATH == ".auto-agent/final_review.json"
    assert wp.PR_REVIEW_PATH == ".auto-agent/pr_review.json"
    assert wp.SMOKE_RESULT_PATH == ".auto-agent/smoke_result.json"
    assert wp.ARCHITECT_LOG_PATH == ".auto-agent/architect_log.md"


def test_review_path_composes_with_item_id() -> None:
    assert wp.review_path("item-3") == ".auto-agent/reviews/item-3.json"
    assert wp.review_path("T1") == ".auto-agent/reviews/T1.json"


def test_decision_history_path_uses_sequence() -> None:
    assert wp.decision_history_path(0) == ".auto-agent/decisions/0.json"
    assert wp.decision_history_path(7) == ".auto-agent/decisions/7.json"


def test_slice_dir_for_sub_architect_namespace() -> None:
    assert wp.slice_dir("auth") == ".auto-agent/slices/auth"
    assert wp.slice_dir("payments") == ".auto-agent/slices/payments"


def test_slice_design_and_backlog_paths() -> None:
    assert wp.slice_design_path("auth") == ".auto-agent/slices/auth/design.md"
    assert wp.slice_backlog_path("auth") == ".auto-agent/slices/auth/backlog.json"


def test_slice_grill_relay_paths() -> None:
    """Sub-architect ↔ parent grill relay paths — §10."""

    assert wp.slice_grill_question_path("auth") == ".auto-agent/slices/auth/grill_question.json"
    assert wp.slice_grill_answer_path("auth") == ".auto-agent/slices/auth/grill_answer.json"


def test_all_paths_are_relative_no_leading_slash() -> None:
    """The orchestrator joins with workspace root — these must be relative."""

    constants = [
        wp.GRILL_PATH,
        wp.PLAN_PATH,
        wp.PLAN_APPROVAL_PATH,
        wp.DESIGN_PATH,
        wp.BACKLOG_PATH,
        wp.DECISION_PATH,
        wp.FINAL_REVIEW_PATH,
        wp.PR_REVIEW_PATH,
        wp.SMOKE_RESULT_PATH,
        wp.ARCHITECT_LOG_PATH,
    ]
    helpers = [
        wp.review_path("x"),
        wp.decision_history_path(1),
        wp.slice_dir("s"),
        wp.slice_design_path("s"),
        wp.slice_backlog_path("s"),
        wp.slice_grill_question_path("s"),
        wp.slice_grill_answer_path("s"),
    ]
    for path in constants + helpers:
        assert not path.startswith("/"), path
        assert path.startswith(wp.AUTO_AGENT_DIR + "/") or path == wp.AUTO_AGENT_DIR
