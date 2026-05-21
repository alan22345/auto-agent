"""Pipeline integration tests for ADR-016 Phase 3.

Phase 2 tests in ``test_graph_pipeline.py`` exercise the AST-only path.
These tests pin the new behaviour:

* ``run_pipeline(..., provider=None)`` keeps the Phase 2 behaviour
  bit-for-bit — no LLM call, only AST edges.
* When a ``provider`` is supplied, unresolved sites are funnelled
  through a single one-shot ``gap_fill_site`` call (no multi-turn agent
  escape) and the surviving validated edges land in the blob with
  ``source_kind="llm"``.
* Edges that fail citation or target validation are dropped before they
  reach the blob.
* Empty one-shot result for a site simply yields no LLM edge for that
  site — no fallback retry burns budget.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from agent.llm.types import LLMResponse, Message, TokenUsage

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_python"


def _setup_workspace(tmp_path: Path) -> str:
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


def _provider_returning_one_shot(payload: dict) -> MagicMock:
    """Provider that always returns the same JSON one-shot reply."""
    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            message=Message(role="assistant", content=json.dumps(payload)),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        ),
    )
    return provider


@pytest.mark.asyncio
async def test_no_provider_skips_gap_fill(tmp_path: Path) -> None:
    """When ``provider=None`` the pipeline behaves exactly as Phase 2 —
    only AST edges, no LLM edges."""
    ws = _setup_workspace(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=None)
    assert all(e.source_kind == "ast" for e in blob.edges)


@pytest.mark.asyncio
async def test_gap_fill_emits_llm_edge_for_registry_site(tmp_path: Path) -> None:
    """The fixture's ``agent_area/registry.py`` has the dispatch site
    ``HANDLERS[name](payload)``. With a mocked provider that cites the
    snippet at the correct line, the blob carries a validated LLM edge."""
    ws = _setup_workspace(tmp_path)
    # The site lives on line 35 of registry.py (the ``return HANDLERS[name](payload)`` line).
    provider = _provider_returning_one_shot(
        {
            "edges": [
                {
                    "target_node_id": "agent_area/registry.py::ping_handler",
                    "evidence_line": 35,
                    "evidence_snippet": "return HANDLERS[name](payload)",
                },
            ],
        },
    )
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    llm_edges = [e for e in blob.edges if e.source_kind == "llm"]
    assert any(
        e.target == "agent_area/registry.py::ping_handler"
        and e.source == "agent_area/registry.py::dispatch"
        and e.evidence.file == "agent_area/registry.py"
        and e.evidence.line == 35
        for e in llm_edges
    )


@pytest.mark.asyncio
async def test_failed_citation_drops_edge(tmp_path: Path) -> None:
    """LLM cites a snippet that does not exist on the cited line (or
    within ±2). Edge must not survive."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot(
        {
            "edges": [
                {
                    "target_node_id": "agent_area/registry.py::ping_handler",
                    "evidence_line": 30,
                    "evidence_snippet": "this snippet is not in the file",
                },
            ],
        },
    )
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    # No LLM edge survives.
    llm_edges = [e for e in blob.edges if e.source_kind == "llm"]
    assert llm_edges == []


@pytest.mark.asyncio
async def test_failed_target_drops_edge(tmp_path: Path) -> None:
    """LLM cites a target id that is not in the graph. The gap-fill
    soft filter would drop it inside the gap_fill module — but the
    unconditional ``validate_target`` is the *load-bearing* check; we
    pin it here by going through the pipeline with a target the gap-
    fill soft filter accidentally let through (we inject via the
    escape path with a candidate-list-accepted-but-graph-absent id).

    Since both filters agree, the easier-to-construct case is: the LLM
    cites a target outside the candidate pool. This validates that the
    final blob has no LLM edge."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot(
        {
            "edges": [
                {
                    "target_node_id": "agent_area/registry.py::nonexistent_target",
                    "evidence_line": 35,
                    "evidence_snippet": "return HANDLERS[name](payload)",
                },
            ],
        },
    )
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    assert [e for e in blob.edges if e.source_kind == "llm"] == []


@pytest.mark.asyncio
async def test_empty_gap_fill_does_not_trigger_fallback(tmp_path: Path) -> None:
    """One-shot returns empty edges → site simply has no LLM edge. No
    multi-turn agent-escape fallback fires. This is the load-bearing
    cost-discipline change in this redesign: previously every empty
    one-shot burned up to 5 turns x 60 s in the bounded agent-escape
    loop, which is what caused the ~12-sites/hour stall on cardamon."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot({"edges": []})
    await run_pipeline(workspace=ws, commit_sha="x", provider=provider)

    # Inspect every recorded call — none must have been a tools-bearing
    # agent-escape invocation. (gap_fill_site calls complete_json which
    # only ever passes tools=None.)
    for call in provider.complete.await_args_list:
        assert call.kwargs.get("tools") in (None, []), (
            f"gap-fill stage made a tools-bearing call: {call.kwargs}"
        )


@pytest.mark.asyncio
async def test_on_progress_callback_fires_per_site(tmp_path: Path) -> None:
    """``run_pipeline`` exposes an ``on_progress`` hook so callers (the
    refresh lifecycle handler) can report live gap-fill progress to the
    UI without the pipeline depending on the orchestrator. The callback
    receives ``(done, total)`` and must fire at least once per completed
    site, with ``done`` monotonically rising to ``total``."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot({"edges": []})

    progress_calls: list[tuple[int, int]] = []

    async def on_progress(done: int, total: int) -> None:
        progress_calls.append((done, total))

    await run_pipeline(
        workspace=ws,
        commit_sha="x",
        provider=provider,
        on_progress=on_progress,
    )

    # Fixture has 2 unresolved sites — pin the contract.
    assert len(progress_calls) >= 2
    # Every callback reports the same total.
    totals = {t for _, t in progress_calls}
    assert totals == {2}
    # done values are unique and monotone.
    done_values = [d for d, _ in progress_calls]
    assert done_values == sorted(done_values)
    assert done_values[-1] == 2


@pytest.mark.asyncio
async def test_gap_fill_stage_runs_sites_concurrently(tmp_path: Path) -> None:
    """Sites are independent — the stage must dispatch them concurrently
    so wall-clock scales with the slowest site, not the sum. We assert
    this by stalling the mock and tracking peak in-flight calls."""
    ws = _setup_workspace(tmp_path)

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def stalled_respond(**kwargs):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            # Long enough that a sequential implementation would never
            # show >1 in flight, short enough to keep the test fast.
            await asyncio.sleep(0.05)
        finally:
            async with lock:
                in_flight -= 1
        return LLMResponse(
            message=Message(role="assistant", content=json.dumps({"edges": []})),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )

    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=stalled_respond)

    # The fixture has at least one unresolved site; if it has more this
    # test gets even sharper. We only assert peak >= 2 to keep the bar
    # robust in CI.
    # First, force the fixture to have ≥2 sites by checking the no-LLM
    # blob's status counts.
    from agent.graph_analyzer.pipeline import run_pipeline as _rp

    baseline = await _rp(workspace=ws, commit_sha="x", provider=None)
    total_sites = sum(a.unresolved_dynamic_sites for a in baseline.areas)
    if total_sites < 2:
        pytest.skip(
            f"fixture only has {total_sites} unresolved sites; concurrency test needs ≥2",
        )

    await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    assert peak >= 2, f"gap-fill stage ran serially: peak={peak}"


@pytest.mark.asyncio
async def test_ast_edges_preserved_alongside_llm_edges(tmp_path: Path) -> None:
    """The gap-fill stage adds to, does not replace, the AST edge set."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot({"edges": []})  # no LLM edges
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    ast_edges = [e for e in blob.edges if e.source_kind == "ast"]
    # Dog inherits Animal — AST edge must still be there.
    assert any(
        e.kind == "inherits" and e.target == "module:agent_area.base.Animal" for e in ast_edges
    )


@pytest.mark.asyncio
async def test_area_status_counts_unresolved_sites(tmp_path: Path) -> None:
    """``AreaStatus.unresolved_dynamic_sites`` is the count of detected
    sites — independent of whether gap-fill produced edges for them."""
    ws = _setup_workspace(tmp_path)
    provider = _provider_returning_one_shot({"edges": []})  # gap-fill no-op
    blob = await run_pipeline(workspace=ws, commit_sha="x", provider=provider)
    statuses = {a.name: a for a in blob.areas}
    # registry.py's HANDLERS[name](payload) is at minimum one site.
    assert statuses["agent_area"].unresolved_dynamic_sites >= 1
