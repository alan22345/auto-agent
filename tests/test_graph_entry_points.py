"""Entry-point detection for the capability/flow map (Phase 1).

Builds synthetic RepoGraphBlobs and asserts the detector returns the
expected EntryPoint list. No real fixtures needed — the detector is a
pure function over the blob.
"""
from __future__ import annotations

from datetime import UTC, datetime

from agent.graph_analyzer.entry_points import detect_entry_points
from shared.types import (
    Edge,
    EdgeEvidence,
    Node,
    RepoGraphBlob,
)


def _make_blob(nodes: list[Node], edges: list[Edge]) -> RepoGraphBlob:
    return RepoGraphBlob(
        commit_sha="0" * 40,
        generated_at=datetime.now(tz=UTC),
        analyser_version="test",
        areas=[],
        nodes=nodes,
        edges=edges,
    )


def _fn(node_id: str, **kwargs) -> Node:
    return Node(
        id=node_id,
        kind="function",
        label=node_id.split("::")[-1],
        file=kwargs.get("file", "src/x.py"),
        area=kwargs.get("area", "src"),
        decorators=kwargs.get("decorators", []),
    )


def _edge(source: str, target: str, kind: str) -> Edge:
    return Edge(
        source=source,
        target=target,
        kind=kind,  # type: ignore[arg-type]
        evidence=EdgeEvidence(file="src/x.py", line=1, snippet="x"),
        source_kind="ast",
    )


def test_http_target_node_is_entry_point():
    nodes = [_fn("api/login.py::login"), _fn("web/login.tsx::handleSubmit")]
    edges = [_edge("web/login.tsx::handleSubmit", "api/login.py::login", "http")]
    eps = detect_entry_points(_make_blob(nodes, edges))
    assert len(eps) == 1
    assert eps[0].node_id == "api/login.py::login"
    assert eps[0].kind == "http"


def test_celery_task_decorator_is_queue_entry_point():
    nodes = [_fn("workers/calc.py::compute", decorators=["@celery.task"])]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["queue"]


def test_worker_suffix_name_is_queue_entry_point():
    nodes = [_fn("workers/runner.py::report_worker")]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["queue"]


def test_scheduled_decorator_is_cron_entry_point():
    nodes = [_fn("jobs/cleanup.py::run", decorators=["@scheduled_task('0 0 * * *')"])]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["cron"]


def test_click_command_is_cli_entry_point():
    nodes = [_fn("cli/admin.py::reset_db", decorators=["@click.command()"])]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["cli"]


def test_main_in_dunder_main_is_cli_entry_point():
    nodes = [_fn("pkg/__main__.py::main", file="pkg/__main__.py")]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["cli"]


def test_function_with_no_signals_is_not_entry_point():
    nodes = [_fn("src/lib/helpers.py::format_date")]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert eps == []


def test_one_node_matches_at_most_one_kind():
    # If a function has both an http-edge target and a celery.task
    # decorator (unusual but legal), http wins (more specific signal).
    nodes = [_fn("api/x.py::handler", decorators=["@celery.task"])]
    edges = [_edge("web/x.tsx::call", "api/x.py::handler", "http")]
    eps = detect_entry_points(_make_blob(nodes, edges))
    assert len(eps) == 1
    assert eps[0].kind == "http"


def test_class_kind_node_with_decorator_is_not_entry_point():
    # Sanity: the function-only filter excludes class-kind nodes even
    # if they carry a decorator that would qualify a function.
    node = Node(
        id="src/x.py::WorkerClass",
        kind="class",
        label="WorkerClass",
        file="src/x.py",
        area="src",
        decorators=["@click.command()"],
    )
    eps = detect_entry_points(_make_blob([node], []))
    assert eps == []


def test_class_kind_node_with_worker_suffix_is_not_entry_point():
    # The *_worker name pattern only applies to functions.
    node = Node(
        id="src/x.py::EmissionsWorker",
        kind="class",
        label="EmissionsWorker",
        file="src/x.py",
        area="src",
    )
    eps = detect_entry_points(_make_blob([node], []))
    assert eps == []


def test_queue_beats_cron_when_both_decorators_present():
    # Same function decorated with both @celery.task and @scheduled_*
    # should be classified as queue (higher precedence).
    nodes = [
        _fn(
            "workers/x.py::run",
            decorators=["@celery.task", "@scheduled_task('@hourly')"],
        ),
    ]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["queue"]


def test_cron_beats_cli_when_both_decorators_present():
    nodes = [
        _fn(
            "cli/jobs.py::main",
            file="cli/jobs.py",
            decorators=["@scheduled_task('0 0 * * *')", "@click.command()"],
        ),
    ]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["cron"]
