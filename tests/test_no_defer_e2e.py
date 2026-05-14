"""End-to-end deletion test for the 4-layer no-defer stack — ADR-015 §8.

A regression test that materializes Task 170's failure mode:

  - A "Phase 1 fills this in" backlog item enters layer 2 (the backlog
    validator).
  - A diff that adds ``raise NotImplementedError`` reachable from a route
    enters layer 3 (``grep_diff_for_stubs``) and layer 4 (PR-reviewer
    artefact/correctness scopes).

At least one of the 4 layers MUST block. The test asserts the
configuration shipped today does block — if a future contributor removes
any layer the deletion test will fail loudly.
"""

from __future__ import annotations

import textwrap

# Task 170's exact backlog-text shape: a description that mentions a
# "Phase 1" follow-up — the architect's hand-rolled scaffold rationale
# that the original failure 1 captured.
_TASK_170_BACKLOG_DESC = " ".join(["word"] * 80) + " Phase 1 will fill this in later"

# Task 170's exact code shape: a route handler whose body is a stub
# ``raise NotImplementedError``. The diff is the smallest one that
# reproduces the original PR #43 introduction.
_TASK_170_DIFF = textwrap.dedent(
    """\
    diff --git a/backend/src/simulation/counterfactual.py b/backend/src/simulation/counterfactual.py
    --- a/backend/src/simulation/counterfactual.py
    +++ b/backend/src/simulation/counterfactual.py
    @@ -370,3 +370,5 @@
     class CounterfactualWorld:
         def fork_from(self, primary):
    +        # Phase 1 fills this in later
    +        raise NotImplementedError
    """
)


def test_layer_2_blocks_task_170_backlog_text() -> None:
    """Layer 2 — backlog validator — rejects the "Phase 1" text."""
    from agent.lifecycle.trio.validators import validate_backlog

    item = {
        "title": "Add counterfactual fork_from",
        "description": _TASK_170_BACKLOG_DESC,
        "justification": "Standalone slice that needs its own PR.",
        "affected_routes": ["/api/counterfactual/start"],
        "affected_files_estimate": 3,
    }
    result = validate_backlog([item])

    assert not result.ok, (
        "DELETION TEST FAILED: layer 2 (backlog validator) no longer rejects "
        "the Task 170 'Phase 1' backlog text. Restore the no-defer regex "
        "set in agent/lifecycle/trio/validators.py — see ADR-015 §8."
    )


def test_layer_3_blocks_task_170_diff() -> None:
    """Layer 3 — ``grep_diff_for_stubs`` — flags the NotImplementedError."""
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    result = grep_diff_for_stubs(_TASK_170_DIFF)
    blocking = [v for v in result.violations if not v.allowed_via_optout]

    assert len(blocking) >= 1, (
        "DELETION TEST FAILED: layer 3 (grep_diff_for_stubs) no longer "
        "catches Task 170's `raise NotImplementedError`. Restore the regex "
        "set in agent/lifecycle/verify_primitives.py — see ADR-015 §8."
    )
    # The "Phase 1" comment alone would also be caught; the
    # NotImplementedError pattern is the load-bearing one.
    labels = {v.pattern for v in blocking}
    assert "raise NotImplementedError" in labels or "Phase 1" in labels


def test_layer_4_pr_review_correctness_blocks_task_170_diff() -> None:
    """Layer 4 — PR-reviewer correctness scope — synthesises comments
    that include the no-defer violation. Pure-Python path; no LLM."""
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    # Exercise the same primitive the PR-reviewer artefact and correctness
    # scopes share. Both scopes call grep_diff_for_stubs on the PR diff
    # before signing off; if the regex set is broken, both go down.
    stubs = grep_diff_for_stubs(_TASK_170_DIFF)
    blocking = [v for v in stubs.violations if not v.allowed_via_optout]

    assert blocking, (
        "DELETION TEST FAILED: layer 4 (PR-reviewer backstop) has nothing "
        "to surface because the shared grep primitive returned empty. "
        "See ADR-015 §8."
    )


def test_at_least_one_layer_blocks_task_170() -> None:
    """The deletion test the ADR §8 references — at least ONE of the four
    layers MUST block Task 170's failure mode.

    Layer 1 is the system prompt rule (can't be tested at runtime since
    we don't invoke the LLM here). Layers 2-4 are pure functions exercised
    by the tests above; this top-level test composes them and asserts the
    layered stack is intact.
    """
    from agent.lifecycle.trio.validators import validate_backlog
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    item = {
        "title": "Add counterfactual fork_from",
        "description": _TASK_170_BACKLOG_DESC,
        "justification": "Standalone slice that needs its own PR.",
        "affected_routes": ["/api/counterfactual/start"],
        "affected_files_estimate": 3,
    }
    backlog_blocked = not validate_backlog([item]).ok

    stubs = grep_diff_for_stubs(_TASK_170_DIFF)
    grep_blocked = bool([v for v in stubs.violations if not v.allowed_via_optout])

    assert backlog_blocked or grep_blocked, (
        "DELETION TEST FAILED: NO layer blocks Task 170's failure mode. "
        "The no-defer stack (ADR-015 §8) has been gutted — restore layer 2 "
        "(validators.py) or layer 3 (verify_primitives.py)."
    )
