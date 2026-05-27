"""Tests for label_flow_blob — the public Phase 2 entry point.

These tests mock the per-flow and per-capability LLM helpers and verify
the cache logic + cache-miss path.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent.graph_analyzer.flow_labeler import label_flow_blob
from shared.types import (
    Capability,
    EntryPoint,
    Flow,
    FlowJsonBlob,
    FlowStep,
    Node,
)


def _node(node_id: str) -> Node:
    return Node(
        id=node_id,
        kind="function",
        label=node_id,
        file=f"{node_id}.py",
        area="src",
        line_start=1,
        line_end=3,
    )


def _flow(id_: str = "f1", entry: str = "x", file_hash: str = "h1") -> Flow:
    return Flow(
        id=id_,
        entry_point=EntryPoint(node_id=entry, kind="http"),
        terminal_node_id=entry,
        terminal_kind="response",
        steps=[FlowStep(node_id=entry, depth=0)],
        file_set=[f"{entry}.py"],
        file_set_hash=file_hash,
    )


def _blob(flows: list[Flow], commit: str = "sha:new") -> FlowJsonBlob:
    return FlowJsonBlob(
        capabilities=[],
        flows=flows,
        unreached=[],
        derived_at_commit=commit,
        deriver_version="phase1",
    )


@pytest.mark.asyncio
async def test_cache_hit_skips_per_flow_llm_call(tmp_path: Path):
    """When file_set_hash matches a prior labelled flow, no LLM call."""
    from agent.graph_analyzer import flow_labeler

    flow_label_mock = AsyncMock()
    cap_label_mock = AsyncMock(
        return_value=[
            {"name": "Auth", "description": "Sign-in.", "flow_ids": ["f1"]},
        ]
    )

    prior = _blob(
        [
            Flow(
                id="f1",
                entry_point=EntryPoint(node_id="x", kind="http"),
                terminal_node_id="x",
                terminal_kind="response",
                steps=[FlowStep(node_id="x", depth=0)],
                file_set=["x.py"],
                file_set_hash="h1",
                name="Existing Name",
                description="Existing Desc.",
                labeled_at_commit="sha:old",
            )
        ],
        commit="sha:old",
    )
    new_blob = _blob([_flow(id_="f1", entry="x", file_hash="h1")], commit="sha:new")

    real_lf = flow_labeler._label_flow
    real_lc = flow_labeler._label_capabilities
    flow_labeler._label_flow = flow_label_mock  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = cap_label_mock  # type: ignore[attr-defined]
    try:
        result = await label_flow_blob(
            new_blob,
            prior_blob=prior,
            workspace_root=tmp_path,
            nodes_by_id={"x": _node("x")},
            provider=MagicMock(),
        )
    finally:
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]

    flow_label_mock.assert_not_awaited()  # cache hit, no LLM call
    assert result.flows[0].name == "Existing Name"
    assert result.flows[0].description == "Existing Desc."
    assert result.flows[0].labeled_at_commit == "sha:old"  # preserved


@pytest.mark.asyncio
async def test_cache_miss_calls_llm_and_sets_commit(tmp_path: Path):
    from agent.graph_analyzer import flow_labeler

    (tmp_path / "x.py").write_text("def x(): pass\n")

    flow_label_mock = AsyncMock(return_value=("Login Flow", "Authenticates."))
    cap_label_mock = AsyncMock(
        return_value=[
            {"name": "Auth", "description": "Sign-in.", "flow_ids": ["f1"]},
        ]
    )

    # Prior blob has a flow but with a DIFFERENT file_set_hash — cache miss.
    prior = _blob(
        [
            Flow(
                id="f1",
                entry_point=EntryPoint(node_id="x", kind="http"),
                terminal_node_id="x",
                terminal_kind="response",
                steps=[FlowStep(node_id="x", depth=0)],
                file_set=["x.py"],
                file_set_hash="h_OLD",
                name="Old Name",
                description="Old Desc.",
                labeled_at_commit="sha:old",
            )
        ],
        commit="sha:old",
    )
    new_blob = _blob([_flow(id_="f1", entry="x", file_hash="h_NEW")], commit="sha:new")

    real_lf = flow_labeler._label_flow
    real_lc = flow_labeler._label_capabilities
    flow_labeler._label_flow = flow_label_mock  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = cap_label_mock  # type: ignore[attr-defined]
    try:
        result = await label_flow_blob(
            new_blob,
            prior_blob=prior,
            workspace_root=tmp_path,
            nodes_by_id={"x": _node("x")},
            provider=MagicMock(),
        )
    finally:
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]

    flow_label_mock.assert_awaited_once()
    assert result.flows[0].name == "Login Flow"
    assert result.flows[0].description == "Authenticates."
    assert result.flows[0].labeled_at_commit == "sha:new"


@pytest.mark.asyncio
async def test_capability_cache_hit_preserves_prior_name(tmp_path: Path):
    """When the emitted capability has the same flow_membership_hash as
    a prior capability, prior name + description are preserved."""
    from agent.graph_analyzer import flow_labeler

    # Prior had Auth capability with flow_ids ["f1", "f2"]; its hash:
    prior_hash = (
        "sha256:"
        + hashlib.sha256(
            ",".join(sorted(["f1", "f2"])).encode("utf-8"),
        ).hexdigest()
    )
    prior = FlowJsonBlob(
        flows=[
            Flow(
                id="f1",
                entry_point=EntryPoint(node_id="a", kind="http"),
                terminal_node_id="a",
                terminal_kind="response",
                steps=[FlowStep(node_id="a", depth=0)],
                file_set=[],
                file_set_hash="h",
                name="A",
                description="a.",
                labeled_at_commit="sha:old",
            ),
            Flow(
                id="f2",
                entry_point=EntryPoint(node_id="b", kind="http"),
                terminal_node_id="b",
                terminal_kind="response",
                steps=[FlowStep(node_id="b", depth=0)],
                file_set=[],
                file_set_hash="h",
                name="B",
                description="b.",
                labeled_at_commit="sha:old",
            ),
        ],
        capabilities=[
            Capability(
                id="cap_prior",
                flow_ids=["f1", "f2"],
                flow_membership_hash=prior_hash,
                name="Prior Auth",
                description="Old desc.",
                labeled_at_commit="sha:old",
            ),
        ],
        unreached=[],
        derived_at_commit="sha:old",
        deriver_version="phase1",
    )

    # New emit: same flow_ids → same hash → cache hit.
    flow_label_mock = AsyncMock(return_value=("X", "x."))  # would be used on miss
    cap_label_mock = AsyncMock(
        return_value=[
            {"name": "Newly Generated", "description": "new.", "flow_ids": ["f1", "f2"]},
        ]
    )

    new_blob = _blob(
        [_flow(id_="f1", entry="a", file_hash="h"), _flow(id_="f2", entry="b", file_hash="h")],
        commit="sha:new",
    )

    real_lf = flow_labeler._label_flow
    real_lc = flow_labeler._label_capabilities
    flow_labeler._label_flow = flow_label_mock  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = cap_label_mock  # type: ignore[attr-defined]
    try:
        result = await label_flow_blob(
            new_blob,
            prior_blob=prior,
            workspace_root=tmp_path,
            nodes_by_id={"a": _node("a"), "b": _node("b")},
            provider=MagicMock(),
        )
    finally:
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]

    assert len(result.capabilities) == 1
    # Prior wins because hash matched.
    assert result.capabilities[0].name == "Prior Auth"
    assert result.capabilities[0].description == "Old desc."
    assert result.capabilities[0].labeled_at_commit == "sha:old"


@pytest.mark.asyncio
async def test_capability_grouping_failure_falls_back_to_unlabeled(tmp_path: Path):
    """When BOTH the freeform LLM grouping AND the path-prefix fallback
    return nothing, the result is the Phase 1 'unlabeled' capability
    containing every flow. The path-prefix fallback is robust enough
    that it usually doesn't return [], but if both signal an upstream
    failure the user still sees a non-empty capability."""
    from agent.graph_analyzer import flow_labeler

    real_lf = flow_labeler._label_flow
    real_lc = flow_labeler._label_capabilities
    real_lpp = flow_labeler._label_capabilities_by_path_prefix
    flow_labeler._label_flow = AsyncMock(return_value=("F", "f."))  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = AsyncMock(return_value=[])  # type: ignore[attr-defined]
    flow_labeler._label_capabilities_by_path_prefix = AsyncMock(  # type: ignore[attr-defined]
        return_value=[],
    )

    new_blob = _blob([_flow(id_="f1", entry="x", file_hash="h")])

    try:
        result = await label_flow_blob(
            new_blob,
            prior_blob=None,
            workspace_root=tmp_path,
            nodes_by_id={"x": _node("x")},
            provider=MagicMock(),
        )
    finally:
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]
        flow_labeler._label_capabilities_by_path_prefix = real_lpp  # type: ignore[attr-defined]

    assert len(result.capabilities) == 1
    assert result.capabilities[0].id == "unlabeled"
    assert result.capabilities[0].flow_ids == ["f1"]


@pytest.mark.asyncio
async def test_capability_grouping_falls_back_to_prefix_when_llm_returns_empty(
    tmp_path: Path,
):
    """Cardamon-scale case: the freeform LLM grouping call returns []
    (typically because the response truncated at the token cap), but
    the path-prefix fallback successfully produces capabilities so the
    user never sees an 'unlabeled' bucket."""
    from agent.graph_analyzer import flow_labeler

    real_lf = flow_labeler._label_flow
    real_lc = flow_labeler._label_capabilities
    real_lpp = flow_labeler._label_capabilities_by_path_prefix
    flow_labeler._label_flow = AsyncMock(return_value=("F", "f."))  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = AsyncMock(return_value=[])  # type: ignore[attr-defined]
    flow_labeler._label_capabilities_by_path_prefix = AsyncMock(  # type: ignore[attr-defined]
        return_value=[
            {
                "name": "Agents",
                "description": "Lifecycle of AI agents.",
                "flow_ids": ["f1"],
            },
        ],
    )

    new_blob = _blob([_flow(id_="f1", entry="x", file_hash="h")])

    try:
        result = await label_flow_blob(
            new_blob,
            prior_blob=None,
            workspace_root=tmp_path,
            nodes_by_id={"x": _node("x")},
            provider=MagicMock(),
        )
    finally:
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]
        flow_labeler._label_capabilities_by_path_prefix = real_lpp  # type: ignore[attr-defined]

    assert len(result.capabilities) == 1
    assert result.capabilities[0].name == "Agents"
    assert result.capabilities[0].flow_ids == ["f1"]
    assert result.capabilities[0].id != "unlabeled"


@pytest.mark.asyncio
async def test_labeler_model_persisted_in_blob(tmp_path: Path):
    from agent.graph_analyzer import flow_labeler

    real_lf = flow_labeler._label_flow
    real_lc = flow_labeler._label_capabilities
    flow_labeler._label_flow = AsyncMock(return_value=("F", "f."))  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = AsyncMock(
        return_value=[
            {"name": "X", "description": "x.", "flow_ids": ["f1"]},
        ]
    )

    new_blob = _blob([_flow(id_="f1", entry="x", file_hash="h")])

    try:
        result = await label_flow_blob(
            new_blob,
            prior_blob=None,
            workspace_root=tmp_path,
            nodes_by_id={"x": _node("x")},
            provider=MagicMock(),
            labeler_model="claude-test-model",
        )
    finally:
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]

    assert result.labeler_model == "claude-test-model"
