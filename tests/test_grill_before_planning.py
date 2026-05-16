"""Tests for the grill-before-planning flow.

The agent runs a multi-round Q&A with the user before any plan is written.
State persists on ``task.intake_qa`` (list of {question, answer} dicts).
The grill loop exits when the agent emits ``GRILL_DONE: <reason>``.

These are unit-level tests — driving handle_planning end-to-end would
require stubbing the orchestrator HTTP layer, the LLM provider, the agent
loop, and Redis. We test the state-machine helpers and prompt shape
directly, plus the pure functions that gate grill entry/exit.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.lifecycle.planning import (
    _MAX_GRILL_ROUNDS,
    _extract_grill_done,
    _grill_round_count,
    _should_run_grill,
)
from agent.prompts import (
    GRILL_DONE_MARKER,
    GRILL_DONE_QUESTION_SENTINEL,
    build_grill_phase_prompt,
    build_planning_prompt,
)

# ---------------------------------------------------------------------------
# _extract_grill_done
# ---------------------------------------------------------------------------

def test_extract_grill_done_returns_reason():
    assert _extract_grill_done("GRILL_DONE: enough context, proceeding to plan") == (
        "enough context, proceeding to plan"
    )


def test_extract_grill_done_handles_leading_text():
    out = "Some preamble.\n\nGRILL_DONE: covered all five axes\n\nMore stuff"
    assert _extract_grill_done(out) == "covered all five axes"


def test_extract_grill_done_returns_none_when_absent():
    assert _extract_grill_done("CLARIFICATION_NEEDED: what's the deal?") is None
    assert _extract_grill_done("just some text") is None


def test_extract_grill_done_no_reason_falls_back():
    assert _extract_grill_done("GRILL_DONE:") == "(no reason)"


# ---------------------------------------------------------------------------
# _should_run_grill
# ---------------------------------------------------------------------------

def _task(complexity: str | None, intake_qa=None) -> SimpleNamespace:
    return SimpleNamespace(complexity=complexity, intake_qa=intake_qa)


def test_should_run_grill_for_any_classified_task_with_no_intake_qa():
    """Policy 2026-05-16: grill ALWAYS runs regardless of complexity."""
    assert _should_run_grill(_task("complex")) is True
    assert _should_run_grill(_task("complex_large")) is True
    assert _should_run_grill(_task("simple")) is True
    assert _should_run_grill(_task("simple_no_code")) is True


def test_should_run_grill_even_when_intake_qa_is_empty_list():
    """Old code paths may have set intake_qa=[] as a 'skip' flag. The
    new policy ignores that signal — the only way out of grilling is
    the GRILL_DONE sentinel."""
    assert _should_run_grill(_task("complex", intake_qa=[])) is True
    assert _should_run_grill(_task("simple", intake_qa=[])) is True


def test_should_skip_grill_when_sentinel_present():
    """Sentinel last entry = grilling complete via GRILL_DONE."""
    completed = [
        {"question": "q1", "answer": "a1"},
        {"question": GRILL_DONE_QUESTION_SENTINEL, "answer": "covered enough"},
    ]
    assert _should_run_grill(_task("complex", intake_qa=completed)) is False
    assert _should_run_grill(_task("simple", intake_qa=completed)) is False


def test_should_continue_grill_when_in_progress():
    """Populated intake_qa without sentinel = grilling in progress.

    The agent gets another grill turn so it can ask the next question OR
    emit GRILL_DONE — never go straight to planning.
    """
    in_progress = [{"question": "q1", "answer": "a1"}]
    assert _should_run_grill(_task("complex", intake_qa=in_progress)) is True

    pending = [
        {"question": "q1", "answer": "a1"},
        {"question": "q2", "answer": None},
    ]
    assert _should_run_grill(_task("complex", intake_qa=pending)) is True


def test_should_skip_grill_when_complexity_missing():
    """Defensive: don't grill before the classifier has run."""
    assert _should_run_grill(_task(None)) is False


# ---------------------------------------------------------------------------
# _grill_round_count + _MAX_GRILL_ROUNDS cap
# ---------------------------------------------------------------------------

def test_grill_round_count_ignores_sentinel():
    """Sentinel entries are bookkeeping — they don't count as user rounds."""
    assert _grill_round_count(None) == 0
    assert _grill_round_count([]) == 0
    assert _grill_round_count([{"question": "q1", "answer": "a1"}]) == 1
    assert _grill_round_count([
        {"question": "q1", "answer": "a1"},
        {"question": "q2", "answer": "a2"},
        {"question": GRILL_DONE_QUESTION_SENTINEL, "answer": "done"},
    ]) == 2  # sentinel doesn't count


def test_max_grill_rounds_is_reasonable():
    """The cap should be high enough for normal use, low enough to bound fatigue."""
    assert 5 <= _MAX_GRILL_ROUNDS <= 20


# ---------------------------------------------------------------------------
# build_grill_phase_prompt
# ---------------------------------------------------------------------------

def test_grill_prompt_first_turn_has_no_history():
    prompt = build_grill_phase_prompt(
        title="Add stripe webhook",
        description="Receive Stripe webhooks and persist them.",
        intake_qa=None,
    )
    assert "GRILL phase" in prompt
    assert "skill(name='grill-with-docs')" in prompt
    assert "skill(name='improve-codebase-architecture')" in prompt
    assert "(no questions asked yet)" in prompt
    assert GRILL_DONE_MARKER in prompt
    assert "CLARIFICATION_NEEDED:" in prompt


def test_grill_prompt_renders_prior_qa():
    prompt = build_grill_phase_prompt(
        title="Add webhook",
        description="...",
        intake_qa=[
            {"question": "Which payment provider?", "answer": "Stripe."},
            {"question": "Persist or fan-out?", "answer": "Both."},
        ],
    )
    assert "Q1" in prompt and "Q2" in prompt
    assert "Stripe." in prompt
    assert "Both." in prompt
    assert "(no questions asked yet)" not in prompt


def test_grill_prompt_warns_against_outputting_a_plan():
    prompt = build_grill_phase_prompt("t", "d", intake_qa=None)
    # The agent is told this turn is grilling, not planning.
    assert "DO NOT output a plan" in prompt or "Do NOT output a plan" in prompt


def test_grill_history_hides_sentinel_entry():
    """The GRILL_DONE sentinel is bookkeeping, not visible in the transcript."""
    prompt = build_grill_phase_prompt(
        "t", "d",
        intake_qa=[
            {"question": "real q", "answer": "real a"},
            {"question": GRILL_DONE_QUESTION_SENTINEL, "answer": "covered enough"},
        ],
    )
    assert "real q" in prompt
    assert "real a" in prompt
    assert GRILL_DONE_QUESTION_SENTINEL not in prompt
    assert "covered enough" not in prompt


def test_planning_prompt_hides_sentinel_in_preflight():
    """Same filter applies to the post-grill planning prompt's preflight section."""
    prompt = build_planning_prompt(
        "t", "d",
        intake_qa=[
            {"question": "real q", "answer": "real a"},
            {"question": GRILL_DONE_QUESTION_SENTINEL, "answer": "done"},
        ],
    )
    assert "real q" in prompt
    assert "real a" in prompt
    assert GRILL_DONE_QUESTION_SENTINEL not in prompt


# ---------------------------------------------------------------------------
# build_planning_prompt with intake_qa
# ---------------------------------------------------------------------------

def test_planning_prompt_no_intake_qa_renders_cleanly():
    prompt = build_planning_prompt("t", "d", repo_summary=None, intake_qa=None)
    assert "Pre-flight grill" not in prompt
    assert "{grill_section}" not in prompt


def test_planning_prompt_includes_grilled_qa():
    prompt = build_planning_prompt(
        "t", "d", repo_summary=None,
        intake_qa=[{"question": "What's the seam?", "answer": "Port + 2 adapters."}],
    )
    assert "Pre-flight grill" in prompt
    assert "What's the seam?" in prompt
    assert "Port + 2 adapters." in prompt
    # The agent is told not to re-ask
    assert "do not re-ask" in prompt.lower()


def test_planning_prompt_keeps_architecture_lens_paragraph():
    """Planning prompt always ends with the architecture lens directive."""
    prompt = build_planning_prompt("t", "d")
    assert "improve-codebase-architecture" in prompt
    assert "deletion test" in prompt
    assert "deep modules" in prompt


# ---------------------------------------------------------------------------
# Grill state-machine end-to-end (helper-level)
# ---------------------------------------------------------------------------

def test_grill_round_trip_state_evolution():
    """Simulate three grill rounds + GRILL_DONE; show how intake_qa evolves.

    The grill gate (`_should_run_grill`) is checked at each step to lock
    the contract: grilling continues until the sentinel is appended.
    """
    task = SimpleNamespace(complexity="complex", intake_qa=None)

    # Initial state: never grilled.
    assert _should_run_grill(task) is True

    # Round 1: agent asks q1, handler appends {q1, None}.
    task.intake_qa = [{"question": "q1", "answer": None}]
    # Still grilling (no sentinel yet).
    assert _should_run_grill(task) is True

    # User answers a1 → handle_clarification_response fills in the pending.
    task.intake_qa[-1] = {**task.intake_qa[-1], "answer": "a1"}
    assert _should_run_grill(task) is True

    # Round 2: agent asks q2.
    task.intake_qa.append({"question": "q2", "answer": None})
    assert _should_run_grill(task) is True

    # User answers a2.
    task.intake_qa[-1] = {**task.intake_qa[-1], "answer": "a2"}
    assert _should_run_grill(task) is True

    # Round 3: agent asks q3.
    task.intake_qa.append({"question": "q3", "answer": None})
    # User answers a3.
    task.intake_qa[-1] = {**task.intake_qa[-1], "answer": "a3"}
    assert _should_run_grill(task) is True

    # Round 4: agent emits GRILL_DONE → handler appends sentinel.
    task.intake_qa.append({
        "question": GRILL_DONE_QUESTION_SENTINEL,
        "answer": "covered all five axes",
    })

    # Sentinel present → grilling complete; future calls go straight to plan.
    assert _should_run_grill(task) is False
    assert len(task.intake_qa) == 4
    assert task.intake_qa[-1]["question"] == GRILL_DONE_QUESTION_SENTINEL


# ---------------------------------------------------------------------------
# Architecture-suggestion path: empty intake_qa skips grilling
# ---------------------------------------------------------------------------

def test_architecture_derived_task_still_grills():
    """Policy 2026-05-16: even tasks born with intake_qa=[] (e.g. from
    architecture-mode suggestions where the suggestion text was deemed
    self-contained) now grill the user. The classifier's signal is
    informational; the only way out of grilling is the GRILL_DONE
    sentinel."""
    task = _task("complex", intake_qa=[])
    assert _should_run_grill(task) is True
