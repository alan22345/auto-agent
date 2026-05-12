"""Tests for the with_browser flag in create_default_registry."""
from __future__ import annotations

from agent.tools import create_default_registry


def test_with_browser_registers_browse_url_and_tail():
    r = create_default_registry(with_web=False, readonly=True, with_browser=True)
    names = set(r.names())
    assert "browse_url" in names
    assert "tail_dev_server_log" in names


def test_default_does_not_register_browser_tools():
    r = create_default_registry(with_web=False, readonly=True)
    names = set(r.names())
    assert "browse_url" not in names
    assert "tail_dev_server_log" not in names
