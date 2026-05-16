"""Tests for agent/tools/__init__.py."""

from agent.tools import create_default_registry


def test_default_registry_excludes_web_tools_by_default():
    reg = create_default_registry(readonly=True)
    names = set(reg.names())
    assert "web_search" not in names
    assert "fetch_url" not in names


def test_default_registry_includes_web_tools_when_requested():
    reg = create_default_registry(readonly=True, with_web=True)
    names = set(reg.names())
    assert "web_search" in names
    assert "fetch_url" in names


def test_default_registry_includes_query_repo_graph_in_readonly_mode():
    """``query_repo_graph`` is read-only and must be available in both
    coding and planning/readonly registries (ADR-016 Phase 6)."""
    readonly_reg = create_default_registry(readonly=True)
    coding_reg = create_default_registry(readonly=False)
    assert "query_repo_graph" in readonly_reg.names()
    assert "query_repo_graph" in coding_reg.names()
