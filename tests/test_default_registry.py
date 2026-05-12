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
