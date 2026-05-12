"""Tests for _extract_affected_routes — parsing the planner's affected-routes block."""

from __future__ import annotations

from agent.lifecycle.planning import _extract_affected_routes


def test_extract_routes_from_fenced_block():
    plan_text = """
    ## Plan
    Adds a dark mode toggle to the homepage settings panel.

    ```affected-routes
    [
      {"method": "GET", "path": "/", "label": "homepage"},
      {"method": "GET", "path": "/settings", "label": "settings page"}
    ]
    ```
    """
    routes = _extract_affected_routes(plan_text)
    assert len(routes) == 2
    assert routes[0]["path"] == "/"


def test_extract_empty_when_no_block():
    routes = _extract_affected_routes("plan with no routes block")
    assert routes == []


def test_extract_empty_when_block_is_empty_list():
    routes = _extract_affected_routes("```affected-routes\n[]\n```")
    assert routes == []
