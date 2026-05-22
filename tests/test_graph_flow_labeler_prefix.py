"""Tests for the URL-prefix capability grouping fallback (Phase 2 §4).

Kicks in when the freeform LLM grouping call returns nothing — typical
for repos large enough that the response would truncate at the
``_CAPABILITY_LABEL_MAX_TOKENS`` cap.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer.flow_labeler import (
    _label_capabilities_by_path_prefix,
    _path_group_key,
)
from shared.types import EntryPoint, Flow, FlowStep


def _flow(id_: str, entry_node_id: str, name: str | None = None) -> Flow:
    return Flow(
        id=id_,
        entry_point=EntryPoint(node_id=entry_node_id, kind="http"),
        terminal_node_id=entry_node_id,
        terminal_kind="response",
        steps=[FlowStep(node_id=entry_node_id, depth=0)],
        file_set=[entry_node_id.split("::", 1)[0]],
        file_set_hash=f"sha256:{id_}",
        name=name,
    )


# ---------------------------------------------------------------------------
# _path_group_key — pure, no LLM
# ---------------------------------------------------------------------------


def test_path_group_key_nextjs_app_router_v1():
    # Skips ``app``, ``api``, ``v1``; ``[id]`` is a dynamic segment;
    # returns the first user-meaningful segment.
    assert (
        _path_group_key("app/api/v1/agents/[id]/route.ts::GET") == "agents"
    )


def test_path_group_key_nextjs_app_router_no_v1():
    assert (
        _path_group_key("app/api/billing/checkout/route.ts::POST") == "billing"
    )


def test_path_group_key_nextjs_admin():
    assert (
        _path_group_key("app/api/v1/admin/dashboard/route.ts::GET") == "admin"
    )


def test_path_group_key_python_module():
    # Python repos: returns the first directory segment (filename
    # itself is skipped — it contains a ``.``).
    assert _path_group_key("orchestrator/router.py::list_repos") == "orchestrator"


def test_path_group_key_drops_dynamic_segments():
    assert (
        _path_group_key(
            "app/api/v1/concierge/sessions/[id]/messages/route.ts::POST"
        )
        == "concierge"
    )


def test_path_group_key_falls_back_to_other_for_pure_filename():
    # File at the repo root — every meaningful part is the filename
    # (filtered out by the ``.`` rule), so we fall back to "other".
    assert _path_group_key("main.py::run") == "other"


# ---------------------------------------------------------------------------
# _label_capabilities_by_path_prefix — wires the grouping + LLM naming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefix_grouping_buckets_flows_then_names_each_via_llm():
    from agent.graph_analyzer import flow_labeler

    flows = [
        _flow("a", "app/api/billing/checkout/route.ts::POST", name="Checkout"),
        _flow("b", "app/api/billing/portal/route.ts::POST", name="Portal"),
        _flow("c", "app/api/v1/agents/[id]/route.ts::GET", name="Get Agent"),
        _flow("d", "app/api/v1/agents/route.ts::GET", name="List Agents"),
        _flow("e", "app/api/health/route.ts::GET", name="Health"),
    ]

    naming_response = {
        "groups": [
            {
                "prefix": "agents",
                "name": "AI Agents",
                "description": "Lifecycle management of AI agents.",
            },
            {
                "prefix": "billing",
                "name": "Billing",
                "description": "Stripe checkout + customer-portal flows.",
            },
            {
                "prefix": "health",
                "name": "Health",
                "description": "Liveness + readiness probes.",
            },
        ],
    }

    flow_labeler.complete_json = AsyncMock(return_value=naming_response)  # type: ignore[attr-defined]
    try:
        caps = await _label_capabilities_by_path_prefix(MagicMock(), flows)
    finally:
        from agent.llm.structured import complete_json as real

        flow_labeler.complete_json = real  # type: ignore[attr-defined]

    by_name = {c["name"]: c for c in caps}
    assert set(by_name) == {"AI Agents", "Billing", "Health"}
    assert set(by_name["AI Agents"]["flow_ids"]) == {"c", "d"}
    assert set(by_name["Billing"]["flow_ids"]) == {"a", "b"}
    assert by_name["Health"]["flow_ids"] == ["e"]
    # Every flow is covered by exactly one capability.
    covered = {fid for c in caps for fid in c["flow_ids"]}
    assert covered == {f.id for f in flows}


@pytest.mark.asyncio
async def test_prefix_grouping_still_returns_groups_when_llm_naming_fails():
    """If the LLM naming call fails, fall back to the bare prefix as
    the capability name so the user sees real groups, not an empty
    list (which would punt back to ``unlabeled``)."""
    from agent.graph_analyzer import flow_labeler

    flows = [
        _flow("a", "app/api/billing/checkout/route.ts::POST"),
        _flow("b", "app/api/v1/agents/route.ts::GET"),
    ]

    flow_labeler.complete_json = AsyncMock(  # type: ignore[attr-defined]
        side_effect=ValueError("model returned malformed JSON"),
    )
    try:
        caps = await _label_capabilities_by_path_prefix(MagicMock(), flows)
    finally:
        from agent.llm.structured import complete_json as real

        flow_labeler.complete_json = real  # type: ignore[attr-defined]

    names = {c["name"] for c in caps}
    assert names == {"Billing", "Agents"}
    # Description carries a defaulted human-readable hint.
    for c in caps:
        assert isinstance(c["description"], str)
        assert "prefix" in c["description"]


@pytest.mark.asyncio
async def test_prefix_grouping_empty_input_returns_empty_list():
    caps = await _label_capabilities_by_path_prefix(MagicMock(), [])
    assert caps == []
