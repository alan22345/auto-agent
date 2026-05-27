"""Pin the scope-maximizing contract on the intent-grill system prompt.

Regression context: a 29KB brief that listed "Phase 1/2/3/4/5" build orders
made the intent-grill agent infer "this task is the scaffold phase only"
and defer every feature to "later phases" — the freeform run produced
foundation-only domains (501 stubs everywhere). The fix was to rewrite
INTENT_GRILL_SYSTEM so phases/build-order sections are interpreted as
HINTS about implementation order, not scope cuts.

If a future edit accidentally drops these guardrails, this test fails.
"""

from __future__ import annotations

from agent.lifecycle.scaffold.prompts import INTENT_GRILL_SYSTEM


def test_intent_grill_prompt_treats_phases_as_hints_not_scope_cuts():
    """The prompt must explicitly call out that phased build orders are not scope limits."""
    text = INTENT_GRILL_SYSTEM.lower()
    # The contract section header
    assert "scope contract" in text, "scope-contract section missing from intent-grill prompt"
    # Phases are hints, not scope cuts
    assert "implementation order" in text or "hints about" in text, (
        "intent-grill prompt should describe phases/build-order sections as hints, not scope limits"
    )
    # Explicit: don't defer feature work
    assert "do not defer" in text, (
        "intent-grill prompt should explicitly forbid deferring feature work to 'later phases'"
    )


def test_intent_grill_prompt_rejects_stub_only_deliverables():
    """501 stubs / empty agent shells should be called out as unacceptable when the brief
    describes real behaviour."""
    text = INTENT_GRILL_SYSTEM.lower()
    # The prompt should warn about stub-only output
    assert "501" in text or "stub" in text, (
        "intent-grill prompt should warn that stubbing out described behaviour is not acceptable"
    )


def test_intent_grill_prompt_drops_smallest_set_language():
    """Old prompt said 'the smallest set that makes this thing recognisable as itself' —
    that bias produced the foundation-only mis-scope. New prompt must not narrow to a
    minimum subset."""
    text = INTENT_GRILL_SYSTEM.lower()
    assert "smallest set" not in text, (
        "intent-grill prompt still contains the scope-minimizing 'smallest set' bias"
    )
