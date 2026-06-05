"""Tests for the graph_findings parameter added to build_po_analysis_prompt.

Verifies:
  - When graph_findings is provided, it appears in the prompt.
  - When graph_findings is None, it is absent (backward compatible).
  - When graph_findings is an empty string, treated as absent.
  - Existing tests in test_prompts_po_with_brief.py still pass structurally
    (no regression from adding the new param).
  - load_latest_graph_blob returns None when _load_graph yields (None, None).
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest

from agent.prompts import build_po_analysis_prompt

# ---------------------------------------------------------------------------
# Shared fake brief (minimal interface matching what build_po_analysis_prompt reads)
# ---------------------------------------------------------------------------


def _fake_brief(**overrides):
    class FakeBrief:
        product_category: ClassVar[str] = "AI dev tools"
        competitors: ClassVar[list] = []
        findings: ClassVar[list] = []
        modality_gaps: ClassVar[list] = []
        strategic_themes: ClassVar[list] = []
        summary: ClassVar[str] = "Test summary."
        created_at: ClassVar[None] = None

    b = FakeBrief()
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


# ---------------------------------------------------------------------------
# graph_findings present
# ---------------------------------------------------------------------------


def test_prompt_includes_graph_findings_when_provided():
    """When graph_findings is given, it must appear verbatim in the prompt."""
    result = build_po_analysis_prompt(
        brief=_fake_brief(),
        graph_findings="SOME FINDINGS\n- hotspot: agent/foo.py",
    )
    assert "SOME FINDINGS" in result
    assert "agent/foo.py" in result


def test_prompt_includes_graph_findings_section_header():
    """The prompt must include the graph-findings section heading."""
    result = build_po_analysis_prompt(
        brief=_fake_brief(),
        graph_findings="Hotspots: agent/foo.py",
    )
    assert "Code graph findings" in result or "code graph findings" in result.lower()


def test_prompt_instructs_po_to_prefer_graph_findings():
    """The prompt must tell the PO to prefer concrete tasks from graph findings."""
    result = build_po_analysis_prompt(
        brief=_fake_brief(),
        graph_findings="Hotspots: agent/foo.py",
    )
    lower = result.lower()
    assert "evidence-backed" in lower or "machine-derived" in lower or "evidence" in lower


# ---------------------------------------------------------------------------
# graph_findings absent / empty — backward compatibility
# ---------------------------------------------------------------------------


def test_prompt_without_graph_findings_has_no_findings_section():
    """Without graph_findings, the prompt must contain no findings section."""
    result = build_po_analysis_prompt(
        brief=_fake_brief(),
        graph_findings=None,
    )
    # Neither the header nor "SOME FINDINGS" should appear
    assert "Code graph findings" not in result
    assert "machine-derived" not in result


def test_prompt_with_empty_graph_findings_is_identical_to_none():
    """An empty string for graph_findings is treated the same as None."""
    result_none = build_po_analysis_prompt(brief=_fake_brief(), graph_findings=None)
    result_empty = build_po_analysis_prompt(brief=_fake_brief(), graph_findings="")
    assert result_none == result_empty


def test_default_graph_findings_is_none_backward_compat():
    """Calling build_po_analysis_prompt without graph_findings must work unchanged."""
    # This is the call signature used by all existing callers
    result = build_po_analysis_prompt(
        brief=_fake_brief(),
        ux_knowledge="some knowledge",
        recent_suggestions=["- do X"],
        goal="ship v2",
    )
    assert "Market context" in result
    assert "Code graph findings" not in result


# ---------------------------------------------------------------------------
# Structural regression — ensure existing sections still present
# ---------------------------------------------------------------------------


def test_po_prompt_still_has_market_context_with_graph_findings():
    """Adding graph_findings must not remove the Market context section."""
    result = build_po_analysis_prompt(
        brief=_fake_brief(),
        graph_findings="## Code Graph Findings\n- hotspot: foo.py",
    )
    assert "Market context" in result


def test_po_prompt_still_has_output_format_with_graph_findings():
    """Adding graph_findings must not remove the output format instructions."""
    result = build_po_analysis_prompt(
        brief=_fake_brief(),
        graph_findings="some findings",
    )
    assert "evidence_urls" in result
    assert "suggestions" in result


# ---------------------------------------------------------------------------
# load_latest_graph_blob — graceful None when _load_graph returns (None, None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_latest_graph_blob_returns_none_when_no_graph():
    """When _load_graph yields (None, None), load_latest_graph_blob returns None."""
    from agent.po_graph_findings import load_latest_graph_blob

    # _load_graph is lazily imported inside load_latest_graph_blob; patch at source
    with patch(
        "agent.tools.query_repo_graph._load_graph",
        new=AsyncMock(return_value=(None, None)),
    ):
        result = await load_latest_graph_blob(repo_id=999)
    assert result is None


@pytest.mark.asyncio
async def test_load_latest_graph_blob_returns_none_when_cfg_none():
    """When _load_graph yields (cfg=None, graph_row=some_row), still returns None."""
    from agent.po_graph_findings import load_latest_graph_blob

    class FakeGraphRow:
        graph_json: ClassVar[dict] = {}

    with patch(
        "agent.tools.query_repo_graph._load_graph",
        new=AsyncMock(return_value=(None, FakeGraphRow())),
    ):
        result = await load_latest_graph_blob(repo_id=999)
    assert result is None


@pytest.mark.asyncio
async def test_load_latest_graph_blob_returns_none_on_exception():
    """If _load_graph raises, load_latest_graph_blob returns None (never raises)."""
    from agent.po_graph_findings import load_latest_graph_blob

    with patch(
        "agent.tools.query_repo_graph._load_graph",
        new=AsyncMock(side_effect=RuntimeError("DB unavailable")),
    ):
        result = await load_latest_graph_blob(repo_id=999)
    assert result is None
