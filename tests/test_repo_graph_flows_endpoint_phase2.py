"""Phase 2 integration: recompute endpoint calls labeller and persists
labelled output. The labeller is mocked so this test stays hermetic."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.types import (
    Capability,
    Edge,
    EdgeEvidence,
    EntryPoint,
    Flow,
    FlowJsonBlob,
    FlowStep,
    Node,
    RepoGraphBlob,
)


def _make_graph_blob_with_http_edge() -> RepoGraphBlob:
    return RepoGraphBlob(
        commit_sha="sha:new",
        generated_at=datetime.now(tz=UTC),
        analyser_version="test",
        areas=[],
        nodes=[
            Node(
                id="api/x.py::handler",
                kind="function",
                label="handler",
                file="api/x.py",
                line_start=1,
                line_end=3,
                area="api",
            ),
            Node(
                id="web/x.tsx::call",
                kind="function",
                label="call",
                file="web/x.tsx",
                area="web",
            ),
        ],
        edges=[
            Edge(
                source="web/x.tsx::call",
                target="api/x.py::handler",
                kind="http",
                source_kind="ast",
                evidence=EdgeEvidence(file="web/x.tsx", line=1, snippet="fetch"),
            ),
        ],
    )


@pytest.mark.asyncio
async def test_endpoint_calls_labeller_and_persists_labelled_blob(tmp_path: Path):
    """The recompute endpoint should derive the blob, pass it (along
    with prior blob and workspace) into label_flow_blob, and persist
    the labelled result."""
    from orchestrator.router import recompute_graph_flows

    graph_blob = _make_graph_blob_with_http_edge()

    # Existing row carries no flow_json yet (first label).
    row = MagicMock()
    row.graph_json = graph_blob.model_dump(mode="json")
    row.flow_json = None
    row.id = 42

    session = AsyncMock(spec=AsyncSession)
    row_result = MagicMock()
    row_result.scalar_one_or_none = MagicMock(return_value=row)
    session.execute = AsyncMock(return_value=row_result)

    expected_labelled = FlowJsonBlob(
        capabilities=[
            Capability(
                id="cap_0",
                flow_ids=["f1"],
                flow_membership_hash="sha256:test",
                name="Auth",
                description="Sign-in.",
                labeled_at_commit="sha:new",
            ),
        ],
        flows=[
            Flow(
                id="f1",
                entry_point=EntryPoint(node_id="api/x.py::handler", kind="http"),
                terminal_node_id="api/x.py::handler",
                terminal_kind="response",
                steps=[FlowStep(node_id="api/x.py::handler", depth=0)],
                file_set=["api/x.py"],
                file_set_hash="sha256:test",
                name="Login Flow",
                description="Auth.",
                labeled_at_commit="sha:new",
            ),
        ],
        unreached=[],
        derived_at_commit="sha:new",
        deriver_version="phase1",
        labeler_model="claude-haiku-4-5",
    )

    labeller_mock = AsyncMock(return_value=expected_labelled)

    with (
        patch(
            "orchestrator.router._get_repo_in_org",
            AsyncMock(return_value=MagicMock(id=1)),
        ),
        patch(
            "agent.graph_workspace.graph_workspace_path",
            return_value=str(tmp_path),
        ),
        patch(
            "agent.graph_analyzer.flow_labeler.label_flow_blob",
            labeller_mock,
        ),
        patch(
            "agent.llm.get_structured_extractor_provider",
            return_value=MagicMock(),
        ),
    ):
        result = await recompute_graph_flows(
            repo_id=1,
            session=session,
            org_id=1,
        )

    labeller_mock.assert_awaited_once()
    call_kwargs = labeller_mock.call_args.kwargs
    assert call_kwargs.get("prior_blob") is None
    assert row.flow_json is not None
    assert row.flow_json["labeler_model"] == "claude-haiku-4-5"
    assert result.labeled_flow_count == 1
    assert result.flow_count == 1


@pytest.mark.asyncio
async def test_endpoint_passes_prior_blob_on_second_recompute(tmp_path: Path):
    """When the row already has flow_json, it's passed as prior_blob
    so the labeller can apply cache hits."""
    from orchestrator.router import recompute_graph_flows

    prior_labelled = FlowJsonBlob(
        capabilities=[],
        flows=[],
        unreached=[],
        derived_at_commit="sha:old",
        deriver_version="phase1",
        labeler_model="claude-haiku-4-5",
    )

    graph_blob = RepoGraphBlob(
        commit_sha="sha:new",
        generated_at=datetime.now(tz=UTC),
        analyser_version="test",
        areas=[],
        nodes=[],
        edges=[],
    )

    row = MagicMock()
    row.graph_json = graph_blob.model_dump(mode="json")
    row.flow_json = prior_labelled.model_dump(mode="json")

    session = AsyncMock(spec=AsyncSession)
    row_result = MagicMock()
    row_result.scalar_one_or_none = MagicMock(return_value=row)
    session.execute = AsyncMock(return_value=row_result)

    labelled_back = prior_labelled.model_copy(update={"derived_at_commit": "sha:new"})
    labeller_mock = AsyncMock(return_value=labelled_back)

    with (
        patch(
            "orchestrator.router._get_repo_in_org",
            AsyncMock(return_value=MagicMock(id=1)),
        ),
        patch(
            "agent.graph_workspace.graph_workspace_path",
            return_value=str(tmp_path),
        ),
        patch(
            "agent.graph_analyzer.flow_labeler.label_flow_blob",
            labeller_mock,
        ),
        patch(
            "agent.llm.get_structured_extractor_provider",
            return_value=MagicMock(),
        ),
    ):
        await recompute_graph_flows(repo_id=1, session=session, org_id=1)

    labeller_mock.assert_awaited_once()
    call_kwargs = labeller_mock.call_args.kwargs
    assert call_kwargs.get("prior_blob") is not None
    assert call_kwargs["prior_blob"].derived_at_commit == "sha:old"


@pytest.mark.asyncio
async def test_labeled_flow_count_only_counts_named_flows(tmp_path: Path):
    """labeled_flow_count reflects only flows with a non-None name."""
    from orchestrator.router import recompute_graph_flows

    graph_blob = RepoGraphBlob(
        commit_sha="sha:x",
        generated_at=datetime.now(tz=UTC),
        analyser_version="test",
        areas=[],
        nodes=[],
        edges=[],
    )

    row = MagicMock()
    row.graph_json = graph_blob.model_dump(mode="json")
    row.flow_json = None

    session = AsyncMock(spec=AsyncSession)
    row_result = MagicMock()
    row_result.scalar_one_or_none = MagicMock(return_value=row)
    session.execute = AsyncMock(return_value=row_result)

    # Labeller returns two flows: one named, one not.
    partially_labelled = FlowJsonBlob(
        capabilities=[],
        flows=[
            Flow(
                id="f1",
                entry_point=EntryPoint(node_id="n1", kind="http"),
                terminal_node_id="n1",
                terminal_kind="response",
                steps=[FlowStep(node_id="n1", depth=0)],
                file_set=[],
                file_set_hash="h1",
                name="Named Flow",
                description="desc",
                labeled_at_commit="sha:x",
            ),
            Flow(
                id="f2",
                entry_point=EntryPoint(node_id="n2", kind="http"),
                terminal_node_id="n2",
                terminal_kind="response",
                steps=[FlowStep(node_id="n2", depth=0)],
                file_set=[],
                file_set_hash="h2",
                name=None,  # unlabelled
                description=None,
                labeled_at_commit=None,
            ),
        ],
        unreached=[],
        derived_at_commit="sha:x",
        deriver_version="phase1",
        labeler_model="claude-haiku-4-5",
    )

    with (
        patch(
            "orchestrator.router._get_repo_in_org",
            AsyncMock(return_value=MagicMock(id=1)),
        ),
        patch(
            "agent.graph_workspace.graph_workspace_path",
            return_value=str(tmp_path),
        ),
        patch(
            "agent.graph_analyzer.flow_labeler.label_flow_blob",
            AsyncMock(return_value=partially_labelled),
        ),
        patch(
            "agent.llm.get_structured_extractor_provider",
            return_value=MagicMock(),
        ),
    ):
        result = await recompute_graph_flows(repo_id=1, session=session, org_id=1)

    assert result.flow_count == 2
    assert result.labeled_flow_count == 1
