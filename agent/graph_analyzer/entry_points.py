"""Entry-point detection for the capability/flow map (Phase 1).

Given a finished :class:`shared.types.RepoGraphBlob`, return the list of
nodes that should be treated as flow entry points. Four kinds in v1:

* ``http``   — target of an incoming ``kind="http"`` edge (already
  produced by ADR-016 Phase 4 cross-language matching).
* ``queue``  — Celery / RQ / dramatiq decorator OR function name
  matching the ``*_worker``/``*_consumer``/``*_handler`` convention.
* ``cron``   — scheduled-job decorator (``@scheduled_*``, ``@cron.*``,
  ``@app.scheduled_*``).
* ``cli``    — Click decorator OR ``main`` in ``__main__.py`` OR
  ``main`` in a ``cli/`` directory.

When a single node matches multiple signals (e.g. an HTTP handler with
a Celery decorator), the most-specific signal wins; the precedence is
``http > queue > cron > cli``.

This is pure: no I/O, no DB, no LLM. Heuristics are easy to extend —
add a kind by adding a clause; missed entry points land in the
Unreached tray downstream.
"""
from __future__ import annotations

import re

from shared.types import EntryPoint, EntryPointKind, Node, RepoGraphBlob

_QUEUE_DECORATOR_RE = re.compile(
    r"^@(?:celery\.task|app\.task|dramatiq\.actor|rq\.job|worker(?:\.\w+)?)\b",
)
_QUEUE_NAME_RE = re.compile(r"_(worker|consumer|handler)$")
_CRON_DECORATOR_RE = re.compile(
    r"^@(?:scheduled_\w+|cron\.\w+|app\.scheduled_\w+|periodic_task)\b",
)
_CLI_DECORATOR_RE = re.compile(r"^@(?:click\.command|click\.group|app\.command)\b")
_DUNDER_MAIN_RE = re.compile(r"(?:^|/)__main__\.py$")
_CLI_DIR_RE = re.compile(r"(?:^|/)cli/")


def _is_http_entry(node: Node, http_targets: set[str]) -> bool:
    return node.id in http_targets


def _is_queue_entry(node: Node) -> bool:
    if any(_QUEUE_DECORATOR_RE.match(d) for d in node.decorators):
        return True
    return bool(_QUEUE_NAME_RE.search(node.label))


def _is_cron_entry(node: Node) -> bool:
    return any(_CRON_DECORATOR_RE.match(d) for d in node.decorators)


def _is_cli_entry(node: Node) -> bool:
    if any(_CLI_DECORATOR_RE.match(d) for d in node.decorators):
        return True
    if node.label != "main":
        return False
    if not node.file:
        return False
    if _DUNDER_MAIN_RE.search(node.file):
        return True
    return bool(_CLI_DIR_RE.search(node.file))


def detect_entry_points(blob: RepoGraphBlob) -> list[EntryPoint]:
    """Return all entry-point nodes in *blob*, deduped by ``node_id``."""
    http_targets: set[str] = {e.target for e in blob.edges if e.kind == "http"}

    result: list[EntryPoint] = []
    seen: set[str] = set()

    def _add(node: Node, kind: EntryPointKind) -> None:
        if node.id in seen:
            return
        result.append(EntryPoint(node_id=node.id, kind=kind))
        seen.add(node.id)

    # Precedence: http > queue > cron > cli.
    for node in blob.nodes:
        if node.kind != "function":
            continue
        if _is_http_entry(node, http_targets):
            _add(node, "http")
    for node in blob.nodes:
        if node.kind != "function":
            continue
        if _is_queue_entry(node):
            _add(node, "queue")
    for node in blob.nodes:
        if node.kind != "function":
            continue
        if _is_cron_entry(node):
            _add(node, "cron")
    for node in blob.nodes:
        if node.kind != "function":
            continue
        if _is_cli_entry(node):
            _add(node, "cli")

    return result


__all__ = ["detect_entry_points"]
