"""Unit tests for the Phase-13 promptfoo assertions — ADR-015 §16 / Phase 13.

The four new ``eval/fixtures/*`` entries each ship with a Python
assertion that probes a *different* failure mode the redesign forbids.
Because the eval itself needs Bedrock + Postgres + Redis to run end to
end, these unit tests exercise the assertion logic directly against
synthetic agent-provider outputs that mirror the shape
``eval/providers/agent_provider.py`` emits.

Each assertion has two flavours of test:

1. **Pass case** — a synthetic output where the relevant no-defer / gate
   layer fired correctly. The assertion must return ``pass=True``.
2. **Fail case** — a synthetic output where the layer was bypassed
   (mirroring the deletion-test property of the ADR-015 §8 stack: if a
   future contributor removes a layer, this test goes red).

The assertions themselves live in ``eval/assertions/`` so promptfoo can
``file://`` them. Importing them here pins their behaviour as the
canonical interpretation of "did the failure mode trigger".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make ``eval/assertions/`` importable without it being a real package
# (the directory has no ``__init__.py`` because promptfoo loads the files
# by path). Adding it to ``sys.path`` lets pytest import the assertion
# modules by name.
_EVAL_ASSERTIONS_DIR = Path(__file__).resolve().parents[1] / "eval" / "assertions"
if str(_EVAL_ASSERTIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_ASSERTIONS_DIR))


# ---------------------------------------------------------------------------
# Helpers — load the four fixture payloads packaged alongside the test.
# ---------------------------------------------------------------------------


def _fixture_dir(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "eval" / "fixtures" / name


def _load(name: str, filename: str) -> dict:
    path = _fixture_dir(name) / filename
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Fixture 1 — stub-introduction-blocked (ADR-015 §8 4-layer no-defer stack).
# ---------------------------------------------------------------------------


def test_stub_introduction_blocked_passes_when_any_layer_fires() -> None:
    from stub_introduction_blocked import get_assert

    payload = _load("stub-introduction-blocked", "synthetic_output.json")
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is True, verdict
    assert verdict["score"] >= 0.5


def test_stub_introduction_blocked_fails_when_diff_ships_stub() -> None:
    """Deletion-test analogue: when the diff contains ``raise
    NotImplementedError`` AND no layer blocked, the assertion fails."""

    from stub_introduction_blocked import get_assert

    payload = _load("stub-introduction-blocked", "bad_output.json")
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is False, verdict
    assert "stub" in verdict["reason"].lower() or "defer" in verdict["reason"].lower()


def test_stub_introduction_blocked_handles_invalid_output() -> None:
    from stub_introduction_blocked import get_assert

    verdict = get_assert("not json at all", {})
    assert verdict["pass"] is False


# ---------------------------------------------------------------------------
# Fixture 2 — design-approval-required-before-dispatch (ADR-015 §2).
# ---------------------------------------------------------------------------


def test_design_approval_gate_passes_when_state_order_is_correct() -> None:
    from design_approval_required import get_assert

    payload = _load("design-approval-required-before-dispatch", "synthetic_output.json")
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is True, verdict


def test_design_approval_gate_fails_when_dispatch_precedes_approval() -> None:
    from design_approval_required import get_assert

    payload = _load("design-approval-required-before-dispatch", "bad_output.json")
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is False, verdict
    assert "dispatch" in verdict["reason"].lower() or "approval" in verdict["reason"].lower()


def test_design_approval_gate_fails_when_design_md_missing() -> None:
    from design_approval_required import get_assert

    payload = {
        "state_transitions": [
            {"from": "ARCHITECT_DESIGNING", "to": "TRIO_EXECUTING"},
        ],
        "auto_agent_files": {},
    }
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is False
    assert "design.md" in verdict["reason"].lower()


# ---------------------------------------------------------------------------
# Fixture 3 — sub-architect-spawn-parent-answers-grill (ADR-015 §10).
# ---------------------------------------------------------------------------


def test_parent_answers_grill_passes_when_parent_is_answerer() -> None:
    from sub_architect_parent_grill import get_assert

    payload = _load("sub-architect-spawn-parent-answers-grill", "synthetic_output.json")
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is True, verdict


def test_parent_answers_grill_fails_when_standin_answers_instead() -> None:
    """If the gate decision log shows ``po_standin`` or
    ``improvement_standin`` answering a sub-architect grill, the relay is
    broken — the user/standin must NEVER be invoked for parent-grill
    questions (ADR-015 §10)."""

    from sub_architect_parent_grill import get_assert

    payload = _load("sub-architect-spawn-parent-answers-grill", "bad_output.json")
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is False, verdict
    assert "standin" in verdict["reason"].lower() or "parent" in verdict["reason"].lower()


def test_parent_answers_grill_fails_when_no_grill_round_recorded() -> None:
    from sub_architect_parent_grill import get_assert

    payload = {"gate_decisions": [], "grill_rounds": []}
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is False


# ---------------------------------------------------------------------------
# Fixture 4 — freeform-standin-decision-logged (ADR-015 §6 + Phase 12).
# ---------------------------------------------------------------------------


def test_freeform_standin_decision_passes_with_full_audit_row() -> None:
    from freeform_standin_decision_logged import get_assert

    payload = _load("freeform-standin-decision-logged", "synthetic_output.json")
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is True, verdict


def test_freeform_standin_decision_fails_when_audit_row_missing_fields() -> None:
    from freeform_standin_decision_logged import get_assert

    payload = _load("freeform-standin-decision-logged", "bad_output.json")
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is False, verdict


def test_freeform_standin_decision_fails_when_grill_answer_mismatches() -> None:
    """When the persisted GateDecision row's verdict text disagrees with
    the ``grill_answer.json`` file, the audit trail is broken."""

    from freeform_standin_decision_logged import get_assert

    payload = {
        "grill_answer_file": {"answer": "use postgres"},
        "standin_event": {
            "standin_kind": "po",
            "agent_id": "po:42",
            "gate": "grill",
            "decision": "use redis",  # MISMATCH on purpose
            "cited_context": ["repo.product_brief"],
            "fallback_reasons": [],
            "timestamp": "2026-05-14T18:00:00+00:00",
        },
        "gate_decision_row": {
            "task_id": 1,
            "gate": "grill",
            "source": "po_standin",
            "agent_id": "po:42",
            "verdict": "use redis",
            "comments": "",
            "cited_context": ["repo.product_brief"],
            "fallback_reasons": [],
        },
    }
    verdict = get_assert(json.dumps(payload), {})
    assert verdict["pass"] is False
    reason = verdict["reason"].lower()
    assert any(phrase in reason for phrase in ("mismatch", "disagree", "match", "differ")), (
        f"unexpected reason for mismatch case: {verdict['reason']!r}"
    )


# ---------------------------------------------------------------------------
# Cross-cutting — every fixture directory shipped is well-formed.
# ---------------------------------------------------------------------------


_FIXTURES = (
    "stub-introduction-blocked",
    "design-approval-required-before-dispatch",
    "sub-architect-spawn-parent-answers-grill",
    "freeform-standin-decision-logged",
)


@pytest.mark.parametrize("name", _FIXTURES)
def test_each_phase13_fixture_has_required_files(name: str) -> None:
    """Every Phase-13 fixture ships task.md + synthetic_output.json +
    bad_output.json so the assertion has both a pass and a deletion-test
    sample to bind against."""

    d = _fixture_dir(name)
    assert d.is_dir(), f"fixture dir missing: {d}"
    for fname in ("task.md", "synthetic_output.json", "bad_output.json"):
        assert (d / fname).is_file(), f"fixture {name} missing {fname}"


@pytest.mark.parametrize("name", _FIXTURES)
def test_each_phase13_fixture_output_is_valid_json(name: str) -> None:
    for fname in ("synthetic_output.json", "bad_output.json"):
        data = (_fixture_dir(name) / fname).read_text()
        # Will raise if malformed.
        json.loads(data)


def test_phase13_promptfooconfig_lists_the_four_fixtures() -> None:
    """The four Phase-13 fixtures must be wired into
    ``eval/promptfooconfig.yaml`` so promptfoo actually evaluates them
    when ``promptfoo eval`` runs."""

    cfg_path = Path(__file__).resolve().parents[1] / "eval" / "promptfooconfig.yaml"
    cfg_text = cfg_path.read_text()
    for name in _FIXTURES:
        assert name in cfg_text, (
            f"promptfooconfig.yaml does not reference fixture {name!r}; Phase-13 wiring incomplete."
        )


# Belt-and-braces: protect the sys.path manipulation done at module load.
def test_eval_assertions_dir_resolved() -> None:
    assert _EVAL_ASSERTIONS_DIR.is_dir(), f"expected eval/assertions/ at {_EVAL_ASSERTIONS_DIR}"
    # The four assertion modules must exist.
    for fname in (
        "stub_introduction_blocked.py",
        "design_approval_required.py",
        "sub_architect_parent_grill.py",
        "freeform_standin_decision_logged.py",
    ):
        assert (_EVAL_ASSERTIONS_DIR / fname).is_file(), f"missing assertion module: {fname}"
