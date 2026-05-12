"""Tests for agent/prompts.py helpers."""

from agent.prompts import augment_coding_prompt_with_server


def test_augment_inserts_server_block():
    base = "You are an autonomous coding agent."
    out = augment_coding_prompt_with_server(
        base, port=3000, affected_routes=[
            {"method": "GET", "path": "/", "label": "home"},
            {"method": "GET", "path": "/settings", "label": "settings"},
        ],
    )
    assert "http://localhost:3000" in out
    assert "/settings" in out
    assert "browse_url" in out


def test_augment_passthrough_when_no_server():
    base = "You are an autonomous coding agent."
    assert augment_coding_prompt_with_server(base, port=None, affected_routes=[]) == base


def test_augment_passthrough_when_no_routes():
    base = "You are an autonomous coding agent."
    assert augment_coding_prompt_with_server(base, port=3000, affected_routes=[]) == base


def test_augment_passthrough_when_no_port():
    base = "You are an autonomous coding agent."
    assert augment_coding_prompt_with_server(
        base, port=None, affected_routes=[{"path": "/", "label": "home"}]
    ) == base
