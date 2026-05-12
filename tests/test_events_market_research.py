"""Tests for market-research event factories + taxonomy."""

from shared.events import (
    POEventType,
    market_research_completed,
    market_research_failed,
    market_research_started,
)


def test_market_research_started_event_shape():
    e = market_research_started(repo_name="foo")
    assert str(e.type) == "po.market_research_started"
    assert e.payload == {"repo_name": "foo"}


def test_market_research_completed_event_shape():
    e = market_research_completed(
        repo_name="foo", brief_id=42, n_competitors=4, n_findings=7, partial=False,
    )
    assert str(e.type) == "po.market_research_completed"
    assert e.payload == {
        "repo_name": "foo",
        "brief_id": 42,
        "n_competitors": 4,
        "n_findings": 7,
        "partial": False,
    }


def test_market_research_failed_includes_reason():
    e = market_research_failed(repo_name="foo", reason="brave key missing")
    assert str(e.type) == "po.market_research_failed"
    assert e.payload == {"repo_name": "foo", "reason": "brave key missing"}


def test_market_research_failed_omits_blank_reason():
    e = market_research_failed(repo_name="foo")
    assert e.payload == {"repo_name": "foo"}


def test_market_research_types_registered_in_po_enum():
    assert POEventType.MARKET_RESEARCH_STARTED == "po.market_research_started"
    assert POEventType.MARKET_RESEARCH_COMPLETED == "po.market_research_completed"
    assert POEventType.MARKET_RESEARCH_FAILED == "po.market_research_failed"
