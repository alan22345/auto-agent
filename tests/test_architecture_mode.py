"""Tests for architecture-mode (continuous deepening loop).

Architecture Mode is parallel to Freeform/PO mode: per-repo opt-in, runs on
a cron, invokes the improve-codebase-architecture skill, produces
``Suggestion`` rows with ``category="architecture"``. Auto-approved
architecture tasks arrive with ``intake_qa=[]`` to skip the grill phase.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agent.architect_analyzer import _is_due, _parse_analysis_output
from agent.prompts import (
    build_architecture_analysis_prompt,
)

# ---------------------------------------------------------------------------
# _is_due — cron logic for architecture mode
# ---------------------------------------------------------------------------

def _config(last_at: datetime | None, cron: str = "0 9 * * 1") -> SimpleNamespace:
    return SimpleNamespace(architecture_cron=cron, last_architecture_at=last_at)


def test_is_due_when_never_run():
    assert _is_due(_config(None), datetime(2026, 5, 4, 12, 0, tzinfo=UTC)) is True


def test_is_due_when_cron_window_passed():
    """Last run a year ago, weekly cron — definitely due."""
    last = datetime(2025, 5, 1, 9, 0, tzinfo=UTC)
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    assert _is_due(_config(last, "0 9 * * 1"), now) is True


def test_is_not_due_immediately_after_run():
    """Just ran 5 minutes ago — next weekly Monday hasn't fired yet."""
    last = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)  # Mon 9am
    now = datetime(2026, 5, 4, 9, 5, tzinfo=UTC)
    assert _is_due(_config(last, "0 9 * * 1"), now) is False


def test_is_due_handles_naive_datetime():
    """Defensive: if DB returns a naive datetime, treat it as UTC and don't crash."""
    last = datetime(2025, 5, 1, 9, 0)  # naive
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    assert _is_due(_config(last, "0 9 * * 1"), now) is True


def test_is_due_for_minutely_cron():
    last = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    now = last + timedelta(minutes=2)
    assert _is_due(_config(last, "* * * * *"), now) is True


# ---------------------------------------------------------------------------
# _parse_analysis_output — JSON parsing tolerant of fences
# ---------------------------------------------------------------------------

def test_parse_plain_json():
    out = '{"suggestions": [{"title": "Deepen X"}], "architecture_knowledge_update": "..."}'
    parsed = _parse_analysis_output(out)
    assert parsed is not None
    assert parsed["suggestions"][0]["title"] == "Deepen X"


def test_parse_json_inside_markdown_fences():
    out = '```json\n{"suggestions": [], "architecture_knowledge_update": "stuff"}\n```'
    parsed = _parse_analysis_output(out)
    assert parsed is not None
    assert parsed["suggestions"] == []


def test_parse_returns_none_on_garbage():
    assert _parse_analysis_output("not JSON at all") is None
    assert _parse_analysis_output("") is None


# ---------------------------------------------------------------------------
# build_architecture_analysis_prompt
# ---------------------------------------------------------------------------

def test_arch_prompt_loads_skills():
    prompt = build_architecture_analysis_prompt()
    assert "skill(name='improve-codebase-architecture')" in prompt
    assert "skill(name='grill-with-docs')" in prompt


def test_arch_prompt_includes_no_prior_knowledge_default():
    prompt = build_architecture_analysis_prompt(architecture_knowledge=None)
    assert "first architecture pass" in prompt


def test_arch_prompt_includes_prior_knowledge():
    prompt = build_architecture_analysis_prompt(architecture_knowledge="foo modules are shallow")
    assert "foo modules are shallow" in prompt


def test_arch_prompt_lists_recent_to_avoid_dupes():
    prompt = build_architecture_analysis_prompt(
        recent_suggestions=["Deepen Webhook intake module", "Merge logging adapters"],
    )
    assert "Deepen Webhook intake module" in prompt
    assert "Merge logging adapters" in prompt


def test_arch_prompt_demands_strict_json():
    prompt = build_architecture_analysis_prompt()
    assert "STRICT JSON" in prompt
    assert "category" in prompt
    # The agent is told category is always 'architecture'
    assert '"architecture"' in prompt


def test_arch_prompt_uses_repo_adr_path():
    prompt = build_architecture_analysis_prompt()
    # The vendored skill rewrites docs/adr → docs/decisions; the prompt
    # references docs/decisions explicitly so the agent knows where to read.
    assert "docs/decisions/" in prompt


# ---------------------------------------------------------------------------
# Suggestion → Task path: architecture suggestions skip grilling
# ---------------------------------------------------------------------------

def test_intake_qa_default_for_architecture_category():
    """intake_qa_for_suggestion is the single source of truth for the
    suggestion → task pre-grilled contract: architecture → [] (skip grill);
    other → None (grill). Both run.py and orchestrator/router.py use it."""
    from shared.models import (
        PRE_GRILLED_SUGGESTION_CATEGORIES,
        intake_qa_for_suggestion,
    )

    assert "architecture" in PRE_GRILLED_SUGGESTION_CATEGORIES
    assert intake_qa_for_suggestion("architecture") == []
    assert intake_qa_for_suggestion("ux_gap") is None
    assert intake_qa_for_suggestion("feature") is None
    assert intake_qa_for_suggestion("improvement") is None
    assert intake_qa_for_suggestion(None) is None
    assert intake_qa_for_suggestion("") is None
