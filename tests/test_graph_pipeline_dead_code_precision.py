"""Integration: run_pipeline dead-code precision fixes (A/C/F/B).

Builds a tiny workspace inline and asserts the false-positive classes that
plagued the auto-agent self-analysis are gone:

* A — ``from pkg import sub`` no longer mis-flags ``pkg/sub.py`` as unused_file.
* C — a lazily-imported (in-function) module is recognised as imported.
* F — a symbol imported by name (never called) is not flagged unused_export.
* B — a file/symbol referenced only by tests is relabelled, not called dead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from shared.types import RepoGraphBlob

if TYPE_CHECKING:
    from pathlib import Path


def _write(ws: Path, rel: str, text: str) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.mark.asyncio
async def test_dead_code_precision(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    _write(ws, "pkg/__init__.py", "")
    # core imports submodules via the package-import form (Fix A) and is the
    # entry that wires everything, so it has a real consumer (itself imported
    # by run.py below).
    _write(
        ws,
        "pkg/core.py",
        "from pkg import sub\n"
        "def go():\n"
        "    return sub.helper()\n",
    )
    _write(ws, "pkg/sub.py", "def helper():\n    return 1\n")
    # lazy in-function import (Fix C)
    _write(
        ws,
        "pkg/lazyuser.py",
        "def run():\n"
        "    from pkg.deferred import deferred_fn\n"
        "    return deferred_fn()\n",
    )
    _write(ws, "pkg/deferred.py", "def deferred_fn():\n    return 2\n")
    # symbol imported by name but never called (Fix F)
    _write(ws, "pkg/exporter.py", "class Thing:\n    pass\n")
    _write(ws, "pkg/usesthing.py", "from pkg.exporter import Thing\n")
    # referenced only by a test (Fix B)
    _write(ws, "pkg/testonly.py", "def only_for_tests():\n    return 3\n")
    _write(
        ws,
        "tests/test_pkg.py",
        "from pkg.testonly import only_for_tests\n"
        "def test_it():\n    assert only_for_tests() == 3\n",
    )
    # root entry that imports the package modules (so lazyuser/usesthing/core
    # have a production importer and aren't themselves the subject here).
    _write(
        ws,
        "run.py",
        "from pkg import core, lazyuser, usesthing\n"
        "core.go()\n",
    )

    blob = await run_pipeline(workspace=str(ws), commit_sha="precision", provider=None)
    assert isinstance(blob, RepoGraphBlob)

    unused_file = {f.target for f in blob.dead_code if f.kind == "unused_file"}
    unused_export = {f.target for f in blob.dead_code if f.kind == "unused_export"}
    by_target = {f.target: f for f in blob.dead_code}

    # A: pkg/sub.py imported via `from pkg import sub` → not unused_file.
    assert "file:pkg/sub.py" not in unused_file
    # C: pkg/deferred.py imported lazily inside a function → not unused_file.
    assert "file:pkg/deferred.py" not in unused_file
    # F: Thing imported by name (never called) → not unused_export.
    assert "pkg/exporter.py::Thing" not in unused_export

    # B: testonly.py is imported ONLY by a test → still surfaced, but labelled.
    to = by_target.get("file:pkg/testonly.py")
    assert to is not None and to.kind == "unused_file"
    assert "test" in to.reason.lower()
