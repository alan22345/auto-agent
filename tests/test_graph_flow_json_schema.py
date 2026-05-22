"""Schema-shape tests for the FlowJsonBlob Pydantic models (Phase 1).

Phase 1 leaves name/description as None; Phase 2 will populate them.
The schema must accept both shapes so a Phase-1-written blob round-trips
through a Phase-2-aware deserialiser.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from agent.graph_analyzer.flows import DERIVER_VERSION
from shared.types import (
    Capability,
    EntryPoint,
    EntryPointKind,
    Flow,
    FlowJsonBlob,
    FlowStep,
)


def test_entry_point_kind_literal_values():
    # All four kinds defined in the spec §3 step 1
    for kind in get_args(EntryPointKind):
        ep = EntryPoint(node_id="m.f", kind=kind)
        assert ep.kind == kind


def test_entry_point_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        EntryPoint(node_id="m.f", kind="websocket")


def test_flow_step_minimum_shape():
    step = FlowStep(node_id="m.f", depth=0)
    assert step.depth == 0
    assert step.is_branch_root is False  # default
    assert step.is_cycle_back is False


def test_flow_phase1_shape_allows_null_label():
    flow = Flow(
        id="auth_login_a1b2",
        entry_point=EntryPoint(node_id="api.login", kind="http"),
        terminal_node_id="api.login",
        terminal_kind="response",
        steps=[FlowStep(node_id="api.login", depth=0)],
        file_set=["api/login.py"],
        file_set_hash="sha256:abc",
        name=None,
        description=None,
    )
    assert flow.name is None
    assert flow.description is None


def test_capability_phase1_unlabeled_id():
    cap = Capability(
        id="unlabeled",
        flow_ids=["auth_login_a1b2"],
        flow_membership_hash="sha256:def",
        name=None,
        description=None,
    )
    assert cap.id == "unlabeled"
    assert cap.name is None


def test_flow_json_blob_round_trip():
    blob = FlowJsonBlob(
        capabilities=[
            Capability(
                id="unlabeled",
                flow_ids=["auth_login_a1b2"],
                flow_membership_hash="sha256:def",
                name=None,
                description=None,
            ),
        ],
        flows=[
            Flow(
                id="auth_login_a1b2",
                entry_point=EntryPoint(node_id="api.login", kind="http"),
                terminal_node_id="api.login",
                terminal_kind="response",
                steps=[FlowStep(node_id="api.login", depth=0)],
                file_set=["api/login.py"],
                file_set_hash="sha256:abc",
                name=None,
                description=None,
            ),
        ],
        unreached=["m.helper"],
        derived_at_commit="sha:7e9f",
        deriver_version=DERIVER_VERSION,
    )
    again = FlowJsonBlob.model_validate(blob.model_dump())
    assert again == blob
