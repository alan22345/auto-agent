"""Pure-logic coverage for the auto-heal supervisor.

The tick/lease reconciliation and batch draining are integration glue (Redis +
DB + the batch handler) verified by deploy-on-VM. These tests lock the pure
holder-string and config-selection helpers.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent.health_loop import supervisor as sup


def test_holder_roundtrip():
    holder = sup._holder(42)
    assert holder == "health-loop:42"
    assert sup._repo_from_holder(holder) == 42


def test_repo_from_holder_rejects_garbage():
    assert sup._repo_from_holder("health-loop:notanint") is None


@dataclass
class _Cfg:
    repo_id: int
    state: str


@pytest.mark.asyncio
async def test_next_runnable_skips_paused_and_prefers_running(monkeypatch):
    configs = [
        _Cfg(repo_id=1, state="paused"),
        _Cfg(repo_id=2, state="idle"),
        _Cfg(repo_id=3, state="running"),
    ]

    async def fake_list():
        return configs

    monkeypatch.setattr(sup, "list_active_configs", fake_list)
    chosen = await sup._next_runnable_config()
    # The 'running' repo wins over the 'idle' one; paused is excluded.
    assert chosen.repo_id == 3


@pytest.mark.asyncio
async def test_next_runnable_returns_none_when_all_paused(monkeypatch):
    async def fake_list():
        return [_Cfg(repo_id=1, state="paused")]

    monkeypatch.setattr(sup, "list_active_configs", fake_list)
    assert await sup._next_runnable_config() is None
