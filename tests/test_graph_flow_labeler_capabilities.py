"""Tests for capability grouping + labelling (Phase 2)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer.flow_labeler import _label_capabilities
from shared.types import EntryPoint, Flow, FlowStep


def _flow(id_: str, name: str | None) -> Flow:
    return Flow(
        id=id_,
        entry_point=EntryPoint(node_id=f"x/{id_}", kind="http"),
        terminal_node_id=f"x/{id_}",
        terminal_kind="response",
        steps=[FlowStep(node_id=f"x/{id_}", depth=0)],
        file_set=[f"x/{id_}.py"],
        file_set_hash=f"sha256:{id_}",
        name=name,
    )


@pytest.mark.asyncio
async def test_label_capabilities_groups_flows():
    from agent.graph_analyzer import flow_labeler

    flows = [
        _flow("a", "Google Login"),
        _flow("b", "GitHub Login"),
        _flow("c", "Submit Report"),
        _flow("d", "View Dashboard"),
    ]
    expected_response = {
        "capabilities": [
            {"name": "Auth", "description": "Sign-in flows.", "flow_ids": ["a", "b"]},
            {"name": "Reports", "description": "Report submission and viewing.",
             "flow_ids": ["c", "d"]},
        ],
    }

    flow_labeler.complete_json = AsyncMock(return_value=expected_response)  # type: ignore[attr-defined]
    try:
        caps = await _label_capabilities(MagicMock(), flows)
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]

    assert len(caps) == 2
    assert caps[0]["name"] == "Auth"
    assert set(caps[0]["flow_ids"]) == {"a", "b"}
    assert caps[1]["name"] == "Reports"
    assert set(caps[1]["flow_ids"]) == {"c", "d"}


@pytest.mark.asyncio
async def test_label_capabilities_returns_empty_on_failure():
    from agent.graph_analyzer import flow_labeler

    flow_labeler.complete_json = AsyncMock(side_effect=ValueError("nope"))  # type: ignore[attr-defined]
    try:
        caps = await _label_capabilities(MagicMock(), [_flow("a", "Login")])
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]

    assert caps == []


@pytest.mark.asyncio
async def test_label_capabilities_drops_capabilities_with_unknown_flow_ids():
    """If the LLM hallucinates a flow_id not in the input, the
    capability that references it is dropped — every flow_id in every
    output capability must exist in the input."""
    from agent.graph_analyzer import flow_labeler

    flows = [_flow("a", "Login"), _flow("b", "Other")]
    bad_response = {
        "capabilities": [
            {"name": "Auth", "description": "Sign-in.", "flow_ids": ["a", "ghost"]},
            {"name": "Other", "description": "Misc.", "flow_ids": ["b"]},
        ],
    }
    flow_labeler.complete_json = AsyncMock(return_value=bad_response)  # type: ignore[attr-defined]
    try:
        caps = await _label_capabilities(MagicMock(), flows)
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]
    # The "Auth" capability is dropped because "ghost" isn't a real id.
    assert len(caps) == 1
    assert caps[0]["name"] == "Other"


@pytest.mark.asyncio
async def test_label_capabilities_handles_empty_flow_list():
    from agent.graph_analyzer import flow_labeler

    mock = AsyncMock()
    flow_labeler.complete_json = mock  # type: ignore[attr-defined]
    try:
        caps = await _label_capabilities(MagicMock(), [])
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]

    # No flows = no LLM call = no capabilities.
    mock.assert_not_awaited()
    assert caps == []
