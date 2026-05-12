"""Regression test: PO drops non-bug suggestions with empty evidence_urls.

This is the load-bearing test for the bigger-PO/market-research overhaul.
The PO prompt asks the model to drop ungrounded suggestions, but the model
may not always obey. The post-parse filter in agent/po_analyzer.py is the
enforcement mechanism. If this test ever fails, the filter has regressed.
"""

from __future__ import annotations

from agent.po_analyzer import _filter_grounded


def test_filter_keeps_grounded_feature_suggestion():
    kept, dropped = _filter_grounded([
        {"title": "Add voice", "category": "feature",
         "evidence_urls": [{"url": "https://x.example", "title": "X", "excerpt": "v"}]},
    ])
    assert dropped == 0
    assert len(kept) == 1


def test_filter_keeps_bug_with_empty_evidence():
    kept, dropped = _filter_grounded([
        {"title": "Fix crash", "category": "bug", "evidence_urls": []},
    ])
    assert dropped == 0
    assert len(kept) == 1


def test_filter_drops_ungrounded_feature():
    """The 'add a button' suggestion type — this is the bug we are fixing."""
    kept, dropped = _filter_grounded([
        {"title": "Add a small icon", "category": "ux_gap", "evidence_urls": []},
    ])
    assert dropped == 1
    assert kept == []


def test_filter_drops_ungrounded_improvement():
    kept, dropped = _filter_grounded([
        {"title": "Generic polish", "category": "improvement", "evidence_urls": []},
    ])
    assert dropped == 1


def test_filter_mixed_input():
    kept, dropped = _filter_grounded([
        {"title": "Add voice", "category": "feature",
         "evidence_urls": [{"url": "https://x", "title": "", "excerpt": ""}]},
        {"title": "Fix login crash", "category": "bug", "evidence_urls": []},
        {"title": "Reorder buttons", "category": "ux_gap", "evidence_urls": []},
        {"title": "Add multi-modal input", "category": "feature",
         "evidence_urls": [{"url": "https://y", "title": "", "excerpt": ""}]},
    ])
    assert dropped == 1
    titles = {s["title"] for s in kept}
    assert titles == {"Add voice", "Fix login crash", "Add multi-modal input"}
