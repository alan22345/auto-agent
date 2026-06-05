"""Regression test: first-party modules imported by bare leaf name must NOT be
flagged as undeclared_dependency (ADR-016 Phase 10 quality-layer fix).

Root cause being tested: ``first_party_top_levels`` was built by taking only
the top-level path segment (e.g. ``svc`` from ``svc.models``), so a bare
``import models`` produced the target ``module:models``, whose top-level is
``models`` — absent from the set — causing a false-positive undeclared flag.

Fixture layout (tests/fixtures/graph_repo_bare_import_python/):

    pyproject.toml
        [project].dependencies = [requests]

    svc/
        __init__.py
        models.py     # first-party — imported as ``import models`` in app.py
        routes.py     # first-party — imported as ``from routes import handler``

    app.py
        import models          ← bare leaf name of svc/models.py
        from routes import handler  ← bare leaf name of svc/routes.py
        import totallyfake     ← genuinely undeclared external

Expected outcomes:
    - ``models``      NOT in undeclared_dependency targets  (first-party leaf — FP suppressed)
    - ``routes``      NOT in undeclared_dependency targets  (first-party leaf — FP suppressed)
    - ``totallyfake`` IS  in undeclared_dependency targets  (genuine external still caught)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from shared.types import RepoGraphBlob

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_bare_import_python"


def _setup(tmp_path: Path) -> str:
    """Copy the bare-import fixture into tmp_path and return workspace path."""
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)
    return str(target)


@pytest.mark.asyncio
async def test_bare_leaf_imports_not_flagged_undeclared(tmp_path: Path) -> None:
    """First-party leaf names imported bare must not appear in undeclared_dependency."""
    ws = _setup(tmp_path)
    blob = await run_pipeline(workspace=ws, commit_sha="testsha-bare-import", provider=None)

    assert isinstance(blob, RepoGraphBlob)
    assert isinstance(blob.dead_code, list)

    undeclared_targets = {f.target for f in blob.dead_code if f.kind == "undeclared_dependency"}

    # 1. First-party leaf name 'models' (= svc/models.py) must NOT be flagged.
    assert "models" not in undeclared_targets, (
        "False positive: 'models' is a first-party leaf (svc/models.py), "
        "must not be flagged as undeclared_dependency. "
        f"undeclared_dependency targets: {sorted(undeclared_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )

    # 2. First-party leaf name 'routes' (= svc/routes.py) must NOT be flagged.
    assert "routes" not in undeclared_targets, (
        "False positive: 'routes' is a first-party leaf (svc/routes.py), "
        "must not be flagged as undeclared_dependency. "
        f"undeclared_dependency targets: {sorted(undeclared_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )

    # 3. Genuinely-undeclared external 'totallyfake' MUST still be flagged.
    assert "totallyfake" in undeclared_targets, (
        "Expected undeclared_dependency for 'totallyfake' (genuine external); "
        f"got undeclared_dependency targets: {sorted(undeclared_targets)}\n"
        f"All dead_code findings: {[(f.kind, f.target) for f in blob.dead_code]}"
    )
