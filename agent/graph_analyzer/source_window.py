"""Read a clamped window of source from an analyser workspace.

Single owner of the "serve source lines from the graph workspace"
behaviour shared by the side-panel code-preview endpoint
(``GET /repos/{id}/graph/code``) and the ``get_symbol_source`` op on
the ``query_repo_graph`` tool (ADR-023). Both callers must never serve
files outside the workspace root and must never return unbounded
output, so the traversal guard and the byte cap live here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# The line cap mirrors the analyser's per-node window so callers can't
# pull arbitrary slabs of source. The byte cap is a defence-in-depth
# ceiling for binary blobs or runaway-long lines.
SOURCE_WINDOW_MAX_LINES = 500
SOURCE_WINDOW_MAX_BYTES = 50 * 1024

_TRUNCATION_MARKER = "\n... [truncated]\n"


class PathOutsideWorkspaceError(Exception):
    """The requested path escapes the workspace root."""


@dataclass
class SourceWindow:
    """One clamped read result.

    ``lines_read`` is the number of lines actually returned — smaller
    than the requested window when the file ends early.
    ``byte_truncated`` is True when the byte cap cut the content.
    """

    content: str
    lines_read: int
    byte_truncated: bool


def read_source_window(
    workspace_root: str,
    path: str,
    line_start: int,
    line_end: int,
) -> SourceWindow:
    """Return lines ``line_start..line_end`` (1-indexed, inclusive) of
    ``path`` under ``workspace_root``, byte-capped.

    Raises :class:`PathOutsideWorkspaceError` when ``path`` is absolute,
    contains ``..`` segments, or resolves outside the workspace root;
    :class:`FileNotFoundError` when the resolved file doesn't exist;
    :class:`ValueError` on an invalid line range.
    """
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise PathOutsideWorkspaceError(path)
    if line_start < 1 or line_end < line_start:
        raise ValueError(f"invalid line range {line_start}..{line_end}")

    root = os.path.realpath(workspace_root)
    target = os.path.realpath(os.path.join(root, path))
    # Belt-and-braces: even with the segment check above, the resolved
    # real path must still live inside the workspace root (symlinks).
    if not (target == root or target.startswith(root + os.sep)):
        raise PathOutsideWorkspaceError(path)

    if not os.path.isfile(target):
        raise FileNotFoundError(path)

    # Stream-read just the requested window, stopping early so a 10MiB
    # minified file doesn't blow up the worker.
    selected: list[str] = []
    with open(target, encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, start=1):
            if lineno < line_start:
                continue
            if lineno > line_end:
                break
            selected.append(raw)

    content = "".join(selected)
    byte_truncated = False
    encoded = content.encode("utf-8")
    if len(encoded) > SOURCE_WINDOW_MAX_BYTES:
        marker = _TRUNCATION_MARKER.encode()
        cap = SOURCE_WINDOW_MAX_BYTES - len(marker)
        content = encoded[:cap].decode("utf-8", errors="replace") + _TRUNCATION_MARKER
        byte_truncated = True

    return SourceWindow(
        content=content,
        lines_read=len(selected),
        byte_truncated=byte_truncated,
    )
