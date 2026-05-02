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

from agent.main import (
    _SKIP_GRILL_COMPLEXITIES,
    _extract_grill_done,
    _should_run_grill,
)
from agent.prompts import (
    GRILL_DONE_MARKER,
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


def test_should_run_grill_for_complex_task_with_no_intake_qa():
    assert _should_run_grill(_task("complex")) is True


def test_should_skip_grill_for_simple_task():
    assert _should_run_grill(_task("simple")) is False
    assert _should_run_grill(_task("simple_no_code")) is False


def test_should_skip_grill_when_intake_qa_already_set():
    """Empty list means 'grilling complete or skipped'."""
    assert _should_run_grill(_task("complex", intake_qa=[])) is False
    assert _should_run_grill(
        _task("complex", intake_qa=[{"question": "q", "answer": "a"}])
    ) is False


def test_should_skip_grill_when_complexity_missing():
    assert _should_run_grill(_task(None)) is False


def test_skip_grill_complexities_constants():
    """Sanity — grill skips simple AND query/no-code paths."""
    assert "simple" in _SKIP_GRILL_COMPLEXITIES
    assert "simple_no_code" in _SKIP_GRILL_COMPLEXITIES


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
    """Simulate three grill rounds; show how intake_qa evolves.

    Round 1: agent asks q1 → state appends {q1, None}.
    Round 2 (after user answers a1): {q1, a1}; agent asks q2 → append {q2, None}.
    Round 3 (after user answers a2): {q1,a1}, {q2,a2}; agent asks q3 → append {q3, None}.
    Round 4 (after user answers a3): {q1,a1}, {q2,a2}, {q3,a3}; GRILL_DONE.
    """
    intake_qa: list[dict] = []

    # Round 1: agent asks q1
    intake_qa.append({"question": "q1", "answer": None})
    assert intake_qa[-1]["answer"] is None

    # User answers a1 → fill in the last pending
    last = intake_qa[-1]
    intake_qa[-1] = {**last, "answer": "a1"}
    assert intake_qa[-1] == {"question": "q1", "answer": "a1"}

    # Round 2: agent asks q2
    intake_qa.append({"question": "q2", "answer": None})

    # User answers a2
    intake_qa[-1] = {**intake_qa[-1], "answer": "a2"}

    # Round 3: agent asks q3
    intake_qa.append({"question": "q3", "answer": None})

    # User answers a3
    intake_qa[-1] = {**intake_qa[-1], "answer": "a3"}

    # GRILL_DONE — list now holds the full transcript
    assert len(intake_qa) == 3
    assert all(qa["answer"] is not None for qa in intake_qa)
    assert [qa["question"] for qa in intake_qa] == ["q1", "q2", "q3"]


# ---------------------------------------------------------------------------
# Architecture-suggestion path: empty intake_qa skips grilling
# ---------------------------------------------------------------------------

def test_architecture_derived_task_skips_grilling():
    """Tasks created from architecture-mode suggestions arrive with intake_qa=[]
    (empty list = 'grilling complete'). They go straight to planning."""
    task = _task("complex", intake_qa=[])
    assert _should_run_grill(task) is False
