"""Top-level derive_flow_blob composes detection + trace + terminal +
hashing + capability assembly into a FlowJsonBlob.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent.graph_analyzer.flows import derive_flow_blob

if TYPE_CHECKING:
    from pathlib import Path
from shared.types import (
    Edge,
    EdgeEvidence,
    Node,
    RepoGraphBlob,
)


def _blob():
    nodes = [
        Node(id="api/login.py::login", kind="function", label="login",
             file="api/login.py", area="api"),
        Node(id="api/login.py::validate", kind="function", label="validate",
             file="api/login.py", area="api"),
        Node(id="lib/db.py::session.commit", kind="function",
             label="session.commit", file="lib/db.py", area="lib"),
        # Web caller — produces the http edge that marks `login` as an entry.
        Node(id="web/login.tsx::handleSubmit", kind="function",
             label="handleSubmit", file="web/login.tsx", area="web"),
        # Unreached node — no edges in or out.
        Node(id="lib/orphan.py::unused", kind="function", label="unused",
             file="lib/orphan.py", area="lib"),
    ]
    edges = [
        Edge(source="web/login.tsx::handleSubmit",
             target="api/login.py::login", kind="http",
             evidence=EdgeEvidence(file="web/login.tsx", line=1, snippet="fetch"),
             source_kind="ast"),
        Edge(source="api/login.py::login", target="api/login.py::validate",
             kind="calls",
             evidence=EdgeEvidence(file="api/login.py", line=2, snippet="validate()"),
             source_kind="ast"),
        Edge(source="api/login.py::validate",
             target="lib/db.py::session.commit", kind="calls",
             evidence=EdgeEvidence(file="api/login.py", line=3, snippet="commit"),
             source_kind="ast"),
    ]
    return RepoGraphBlob(
        commit_sha="abc123",
        generated_at=datetime.now(tz=UTC),
        analyser_version="test",
        areas=[],
        nodes=nodes,
        edges=edges,
    )


def test_derive_produces_single_flow_from_http_entry():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert len(blob.flows) == 1
    flow = blob.flows[0]
    assert flow.entry_point.kind == "http"
    assert flow.entry_point.node_id == "api/login.py::login"
    assert [s.node_id for s in flow.steps] == [
        "api/login.py::login",
        "api/login.py::validate",
        "lib/db.py::session.commit",
    ]
    assert flow.terminal_kind == "db_write"


def test_flow_id_is_deterministic_hash_of_entry():
    expected = hashlib.sha256(b"api/login.py::login").hexdigest()[:12]
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert blob.flows[0].id == expected


def test_file_set_is_sorted_and_unique():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    flow = blob.flows[0]
    assert flow.file_set == ["api/login.py", "lib/db.py"]


def test_capability_unlabeled_contains_all_flows():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert len(blob.capabilities) == 1
    cap = blob.capabilities[0]
    assert cap.id == "unlabeled"
    assert cap.flow_ids == [f.id for f in blob.flows]
    expected_hash = hashlib.sha256(
        ",".join(sorted(cap.flow_ids)).encode("utf-8"),
    ).hexdigest()
    assert cap.flow_membership_hash == f"sha256:{expected_hash}"


def test_unreached_contains_orphan_node():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert "lib/orphan.py::unused" in blob.unreached
    # Step nodes must NOT appear in unreached.
    for flow in blob.flows:
        for step in flow.steps:
            assert step.node_id not in blob.unreached


def test_file_set_hash_uses_workspace_contents_when_provided(tmp_path: Path):
    # Materialise files matching the blob's references.
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "login.py").write_text("def login(): pass\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "db.py").write_text("session = None\n")
    blob = derive_flow_blob(_blob(), workspace_root=tmp_path)
    flow = blob.flows[0]
    # File-content version of the hash differs from the path-only fallback.
    assert flow.file_set_hash.startswith("sha256:")

    other = derive_flow_blob(_blob(), workspace_root=None)
    assert flow.file_set_hash != other.flows[0].file_set_hash


def test_derived_at_commit_matches_blob_sha():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert blob.derived_at_commit == "abc123"
