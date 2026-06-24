"""Pure-logic coverage for the auto-heal batch handler.

The full fix cycle (clone → code → gates → merge) is integration glue verified
by deploy-on-VM, not here. These tests lock the deterministic helpers that
DON'T need a coder, git, Redis, or the DB.
"""

from __future__ import annotations

import pytest

from agent.health_loop import batch_handler as bh
from agent.health_loop.findings import HealthFinding


def _finding(h: str, category: str = "dead_code", title: str = "x") -> HealthFinding:
    return HealthFinding(
        finding_hash=h, category=category, title=title, files=["a.py"], severity=1.0
    )


def test_batch_hash_is_order_independent_and_stable():
    a = bh._batch_hash(["c", "a", "b"])
    b = bh._batch_hash(["a", "b", "c"])
    assert a == b
    assert len(a) == 12
    # A different membership ⇒ a different hash.
    assert bh._batch_hash(["a", "b"]) != a


def test_build_fix_prompt_lists_every_finding_and_demands_behavior_preservation():
    batch = [_finding("h1", title="cycle A→B"), _finding("h2", title="dead foo")]
    prompt = bh._build_fix_prompt("acme/web", batch)
    assert "acme/web" in prompt
    assert "cycle A→B" in prompt
    assert "dead foo" in prompt
    assert "behavior-preserving" in prompt.lower()


def test_parse_pr_url_extracts_owner_repo_number():
    assert bh._parse_pr_url("https://github.com/acme/web/pull/42") == ("acme", "web", "42")
    assert bh._parse_pr_url("https://github.com/acme/web/pull/42/") == ("acme", "web", "42")


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Returns the PR head SHA, then a canned check-runs payload."""

    def __init__(self, check_runs: list[dict], pr_ok: bool = True):
        self._check_runs = check_runs
        self._pr_ok = pr_ok

    async def get(self, url: str, headers=None):
        if "/pulls/" in url:
            return _FakeResp(200 if self._pr_ok else 404, {"head": {"sha": "deadbeef"}})
        return _FakeResp(200, {"check_runs": self._check_runs})


@pytest.mark.asyncio
async def test_ci_verdict_no_checks_means_no_ci():
    verdict = await bh._ci_verdict_once(_FakeClient([]), "o", "r", "1", {})
    assert verdict == "no_ci"


@pytest.mark.asyncio
async def test_ci_verdict_pending_when_a_run_is_incomplete():
    runs = [{"status": "completed", "conclusion": "success"}, {"status": "in_progress"}]
    verdict = await bh._ci_verdict_once(_FakeClient(runs), "o", "r", "1", {})
    assert verdict is None


@pytest.mark.asyncio
async def test_ci_verdict_failure_when_any_run_failed():
    runs = [{"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "failure"}]
    verdict = await bh._ci_verdict_once(_FakeClient(runs), "o", "r", "1", {})
    assert verdict == "failure"


@pytest.mark.asyncio
async def test_ci_verdict_success_when_all_complete_and_green():
    runs = [{"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "skipped"}]
    verdict = await bh._ci_verdict_once(_FakeClient(runs), "o", "r", "1", {})
    assert verdict == "success"


@pytest.mark.asyncio
async def test_ci_verdict_none_when_pr_fetch_fails():
    verdict = await bh._ci_verdict_once(_FakeClient([], pr_ok=False), "o", "r", "1", {})
    assert verdict is None
