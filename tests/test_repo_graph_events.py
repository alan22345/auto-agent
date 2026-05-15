"""ADR-016 Phase 2 — typed events for the code-graph analyser.

The factories carry payload schemas so a typo at a producer site becomes a
``TypeError`` instead of a silent payload-key drift downstream (ADR-011's
forcing-function pattern).
"""

from __future__ import annotations

from shared.events import (
    Event,
    RepoEventType,
    repo_graph_failed,
    repo_graph_ready,
    repo_graph_requested,
)


class TestRepoGraphRequested:
    def test_payload_carries_repo_id_and_request_id(self) -> None:
        ev = repo_graph_requested(repo_id=42, request_id="abc-123")
        assert ev.type == RepoEventType.GRAPH_REQUESTED
        assert ev.payload == {"repo_id": 42, "request_id": "abc-123"}

    def test_roundtrip_through_redis_serialisation(self) -> None:
        ev = repo_graph_requested(repo_id=7, request_id="r-1")
        wire = ev.to_redis()
        restored = Event.from_redis({"data": wire["data"]})
        assert restored.type == "repo.graph_requested"
        assert restored.payload == {"repo_id": 7, "request_id": "r-1"}


class TestRepoGraphReady:
    def test_payload_shape(self) -> None:
        ev = repo_graph_ready(
            repo_id=1,
            repo_graph_id=99,
            commit_sha="abc",
            status="ok",
        )
        assert ev.type == RepoEventType.GRAPH_READY
        assert ev.payload == {
            "repo_id": 1,
            "repo_graph_id": 99,
            "commit_sha": "abc",
            "status": "ok",
        }

    def test_partial_status_passes_through(self) -> None:
        ev = repo_graph_ready(
            repo_id=1,
            repo_graph_id=5,
            commit_sha="deadbee",
            status="partial",
        )
        assert ev.payload["status"] == "partial"


class TestRepoGraphFailed:
    def test_payload_shape(self) -> None:
        ev = repo_graph_failed(repo_id=3, error="clone failed: auth")
        assert ev.type == RepoEventType.GRAPH_FAILED
        assert ev.payload == {"repo_id": 3, "error": "clone failed: auth"}
