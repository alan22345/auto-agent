"""ADR-020 — architect-as-scope-guardian invariants.

Pins that the scope-guardian block is present in every architect-phase
system prompt and in the gap-fix + final-reviewer user prompts. Without
these references in place, the architect drifts toward fixing whatever
pre-existing bug the final reviewer's smoke check happens to surface —
which is how task 28 (2026-05-27) shipped a counterfactual simulation
subsystem in a "prettier apartment" PR.
"""

from __future__ import annotations


def test_scope_guardian_block_is_in_every_architect_phase_prompt():
    from agent.lifecycle.trio.prompts import (
        ARCHITECT_BACKLOG_EMIT_SYSTEM,
        ARCHITECT_CHECKPOINT_SYSTEM,
        ARCHITECT_DESIGN_SYSTEM,
        ARCHITECT_INITIAL_SYSTEM,
        ARCHITECT_SCOPE_GUARDIAN_BLOCK,
    )

    # The block must be embedded verbatim so a single source-of-truth
    # update propagates to every architect turn.
    for prompt in (
        ARCHITECT_DESIGN_SYSTEM,
        ARCHITECT_BACKLOG_EMIT_SYSTEM,
        ARCHITECT_INITIAL_SYSTEM,
        ARCHITECT_CHECKPOINT_SYSTEM,
    ):
        assert ARCHITECT_SCOPE_GUARDIAN_BLOCK in prompt


def test_scope_guardian_block_names_the_load_bearing_concepts():
    """Without these words the LLM can't apply the rule."""
    from agent.lifecycle.trio.prompts import ARCHITECT_SCOPE_GUARDIAN_BLOCK

    block = ARCHITECT_SCOPE_GUARDIAN_BLOCK
    assert "ADR-020" in block
    assert "design.md" in block
    assert "escalate" in block.lower()
    assert "dispatch_new" in block
    assert "out-of-scope" in block.lower()
    # The task-28 worked example must stay — it's the most concrete
    # signal to the LLM about what NOT to do.
    assert "task 28" in block.lower() or "prettier apartment" in block.lower()


def test_gap_fix_user_prompt_references_scope_check():
    """The gap-fix user prompt reinforces the architect-system rule at
    decision time."""
    from agent.lifecycle.trio.gap_fix import _GAP_FIX_PROMPT

    assert "ADR-020" in _GAP_FIX_PROMPT
    assert "scope" in _GAP_FIX_PROMPT.lower()
    assert "escalate" in _GAP_FIX_PROMPT.lower()


def test_final_reviewer_prompt_filters_gaps_by_scope():
    """The final reviewer is told to scope-filter the gaps it reports.
    A pre-existing bug in code design.md doesn't mention is a
    diagnostic, not a gap."""
    from agent.lifecycle.trio.final_reviewer import _FINAL_REVIEW_PROMPT

    assert "ADR-020" in _FINAL_REVIEW_PROMPT
    assert "scope" in _FINAL_REVIEW_PROMPT.lower()
    # The reviewer must be explicitly told to mention out-of-scope
    # findings in comments rather than gaps.
    assert "comments" in _FINAL_REVIEW_PROMPT.lower()
