"""Smoke test the new MarketBrief ORM model + extended Suggestion/FreeformConfig.

Uses column introspection (no real DB needed) consistent with the existing
pattern in test_models_integrations.py and test_models_phase4.py.
"""
from __future__ import annotations

from shared.models import FreeformConfig, MarketBrief, Suggestion


def test_market_brief_table_name() -> None:
    assert MarketBrief.__tablename__ == "market_briefs"


def test_market_brief_columns() -> None:
    cols = {c.name for c in MarketBrief.__table__.columns}
    assert {
        "id",
        "repo_id",
        "organization_id",
        "created_at",
        "product_category",
        "competitors",
        "findings",
        "modality_gaps",
        "strategic_themes",
        "summary",
        "raw_sources",
        "partial",
        "agent_turns",
    } <= cols


def test_market_brief_fks() -> None:
    fk_targets = {
        next(iter(c.foreign_keys)).target_fullname
        for c in MarketBrief.__table__.columns
        if c.foreign_keys
    }
    assert "repos.id" in fk_targets
    assert "organizations.id" in fk_targets


def test_market_brief_has_repo_relationship() -> None:
    assert hasattr(MarketBrief, "repo")


def test_suggestion_has_evidence_urls_and_brief_columns() -> None:
    cols = {c.name for c in Suggestion.__table__.columns}
    assert "evidence_urls" in cols
    assert "brief_id" in cols


def test_suggestion_brief_fk_points_to_market_briefs() -> None:
    fk_targets = {
        next(iter(c.foreign_keys)).target_fullname
        for c in Suggestion.__table__.columns
        if c.foreign_keys
    }
    assert "market_briefs.id" in fk_targets


def test_suggestion_has_brief_relationship() -> None:
    assert hasattr(Suggestion, "brief")


def test_freeform_config_has_market_brief_columns() -> None:
    cols = {c.name for c in FreeformConfig.__table__.columns}
    assert "last_market_research_at" in cols
    assert "market_brief_max_age_days" in cols


def test_freeform_config_market_brief_max_age_default() -> None:
    col = FreeformConfig.__table__.columns["market_brief_max_age_days"]
    assert col.default.arg == 7
    assert col.nullable is False


def test_freeform_config_last_market_research_at_is_nullable() -> None:
    col = FreeformConfig.__table__.columns["last_market_research_at"]
    assert col.nullable is True
