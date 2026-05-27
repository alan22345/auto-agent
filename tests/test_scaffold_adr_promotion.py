"""Tests for the ADR promote-on-approval helper and its wiring into
the scaffold root/domain ADR approval gates.

When a root or domain ADR is approved, the source file under
``.auto-agent/adrs/`` is *copied* (not moved) into ``docs/decisions/``
so the project's canonical ADR directory holds the approved decision.
The gates keep reading from the original under ``.auto-agent/adrs/``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_scaffold_lifecycle.py)
# ---------------------------------------------------------------------------


def _make_scaffold_task(
    *,
    task_id: int = 1,
    status: str = "awaiting_root_adr_approval",
    subtasks: dict | None = None,
):
    from shared.models import TaskComplexity, TaskSource, TaskStatus

    return SimpleNamespace(
        id=task_id,
        title="Scaffold parent",
        description="Build a TODO app.",
        status=TaskStatus(status),
        complexity=TaskComplexity.SCAFFOLD,
        repo_id=None,
        repo=None,
        freeform_mode=True,
        organization_id=7,
        created_by_user_id=None,
        parent_task_id=None,
        subtasks=subtasks,
        source=TaskSource.MANUAL,
        affected_routes=[],
    )


def _patched_session_factory(task):
    @asynccontextmanager
    async def factory():
        class _Result:
            def __init__(self, t):
                self._t = t

            def scalar_one(self):
                return self._t

        class _Session:
            def __init__(self, t):
                self._t = t

            async def execute(self, *_a, **_kw):
                return _Result(self._t)

            def add(self, _obj):
                pass

            async def commit(self):
                pass

            async def flush(self):
                pass

        yield _Session(task)

    return factory


_ROOT_ADR_MD = """\
# 000 — System ADR

## Vision
A TODO app.

## Cross-cutting concerns
- Auth — JWT.

## Domains

```yaml
domains:
  - name: Auth
    slug: auth
    scope_summary: Authentication and session management.
  - name: Billing
    slug: billing
    scope_summary: Plans, invoices, Stripe webhooks.
```
"""


# ---------------------------------------------------------------------------
# 1. promote_adr_to_docs helper
# ---------------------------------------------------------------------------


def test_promote_adr_to_docs_copies_file_into_docs_decisions(tmp_path: Path):
    from agent.lifecycle.scaffold._promotion import promote_adr_to_docs
    from agent.lifecycle.workspace_paths import ROOT_ADR_PATH

    src = tmp_path / ROOT_ADR_PATH
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# 000 — System ADR\n\nbody\n")

    dest = promote_adr_to_docs(str(tmp_path), ROOT_ADR_PATH)

    expected = tmp_path / "docs" / "decisions" / "000-system.md"
    assert dest == str(expected)
    assert expected.is_file()
    assert expected.read_text() == "# 000 — System ADR\n\nbody\n"


def test_promote_adr_to_docs_returns_none_when_source_missing(tmp_path: Path):
    from agent.lifecycle.scaffold._promotion import promote_adr_to_docs
    from agent.lifecycle.workspace_paths import ROOT_ADR_PATH

    dest = promote_adr_to_docs(str(tmp_path), ROOT_ADR_PATH)

    assert dest is None
    assert not (tmp_path / "docs" / "decisions" / "000-system.md").exists()


def test_promote_adr_to_docs_overwrites_existing_destination(tmp_path: Path):
    from agent.lifecycle.scaffold._promotion import promote_adr_to_docs
    from agent.lifecycle.workspace_paths import ROOT_ADR_PATH

    src = tmp_path / ROOT_ADR_PATH
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("new contents\n")

    dest_path = tmp_path / "docs" / "decisions" / "000-system.md"
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text("stale contents\n")

    promote_adr_to_docs(str(tmp_path), ROOT_ADR_PATH)

    assert dest_path.read_text() == "new contents\n"


# ---------------------------------------------------------------------------
# 2. Root-ADR approval wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_adr_approval_approved_promotes_into_docs_decisions(tmp_path: Path):
    from agent.lifecycle.scaffold import root_adr_approval
    from agent.lifecycle.workspace_paths import ROOT_ADR_PATH

    src = tmp_path / ROOT_ADR_PATH
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(_ROOT_ADR_MD)

    task = _make_scaffold_task()
    with (
        patch.object(root_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(root_adr_approval, "transition", AsyncMock()),
        patch.object(
            root_adr_approval,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
    ):
        await root_adr_approval.apply_verdict(
            task.id, {"verdict": "approved", "comments": "lgtm"}
        )

    promoted = tmp_path / "docs" / "decisions" / "000-system.md"
    assert promoted.is_file()
    assert promoted.read_text() == _ROOT_ADR_MD


@pytest.mark.asyncio
async def test_root_adr_approval_revise_does_not_promote(tmp_path: Path):
    from agent.lifecycle.scaffold import root_adr_approval
    from agent.lifecycle.workspace_paths import ROOT_ADR_PATH

    src = tmp_path / ROOT_ADR_PATH
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(_ROOT_ADR_MD)

    task = _make_scaffold_task(subtasks=None)
    with (
        patch.object(root_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(root_adr_approval, "transition", AsyncMock()),
        patch.object(
            root_adr_approval,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
    ):
        await root_adr_approval.apply_verdict(
            task.id, {"verdict": "revise", "comments": "rework auth"}
        )

    assert not (tmp_path / "docs" / "decisions" / "000-system.md").exists()


@pytest.mark.asyncio
async def test_root_adr_approval_rejected_does_not_promote(tmp_path: Path):
    from agent.lifecycle.scaffold import root_adr_approval
    from agent.lifecycle.workspace_paths import ROOT_ADR_PATH

    src = tmp_path / ROOT_ADR_PATH
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(_ROOT_ADR_MD)

    task = _make_scaffold_task()
    with (
        patch.object(root_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(root_adr_approval, "transition", AsyncMock()),
        patch.object(
            root_adr_approval,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
    ):
        await root_adr_approval.apply_verdict(
            task.id, {"verdict": "rejected", "comments": "scope explosion"}
        )

    assert not (tmp_path / "docs" / "decisions" / "000-system.md").exists()


# ---------------------------------------------------------------------------
# 3. Domain-ADR approval wiring
# ---------------------------------------------------------------------------


def _seed_domain_workspace(workspace: Path, *, with_grill: bool = True) -> None:
    """Lay down a root ADR + auth domain ADR (+ optional grill) under
    ``.auto-agent/adrs/`` in ``workspace``.
    """

    from agent.lifecycle.workspace_paths import (
        ROOT_ADR_PATH,
        domain_adr_path,
        domain_grill_path,
    )

    (workspace / ROOT_ADR_PATH).parent.mkdir(parents=True, exist_ok=True)
    (workspace / ROOT_ADR_PATH).write_text(_ROOT_ADR_MD)
    (workspace / domain_adr_path(1, "auth")).write_text("# 001 — Auth ADR\n")
    if with_grill:
        (workspace / domain_grill_path(1, "auth")).write_text("# Auth grill\n")


@pytest.mark.asyncio
async def test_domain_adr_approval_approved_promotes_adr_and_grill(tmp_path: Path):
    from agent.lifecycle.scaffold import domain_adr_approval

    _seed_domain_workspace(tmp_path, with_grill=True)
    task = _make_scaffold_task(status="awaiting_domain_adr_approval")

    with (
        patch.object(domain_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(domain_adr_approval, "transition", AsyncMock()),
        patch.object(
            domain_adr_approval,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
    ):
        await domain_adr_approval.apply_verdict(
            task.id, "auth", {"verdict": "approved", "comments": "lgtm"}
        )

    promoted_adr = tmp_path / "docs" / "decisions" / "001-auth.md"
    promoted_grill = tmp_path / "docs" / "decisions" / "001-auth.grill.md"
    assert promoted_adr.is_file()
    assert promoted_adr.read_text() == "# 001 — Auth ADR\n"
    assert promoted_grill.is_file()
    assert promoted_grill.read_text() == "# Auth grill\n"


@pytest.mark.asyncio
async def test_domain_adr_approval_approved_promotes_adr_without_grill(tmp_path: Path):
    from agent.lifecycle.scaffold import domain_adr_approval

    _seed_domain_workspace(tmp_path, with_grill=False)
    task = _make_scaffold_task(status="awaiting_domain_adr_approval")

    with (
        patch.object(domain_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(domain_adr_approval, "transition", AsyncMock()),
        patch.object(
            domain_adr_approval,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
    ):
        await domain_adr_approval.apply_verdict(
            task.id, "auth", {"verdict": "approved", "comments": "lgtm"}
        )

    assert (tmp_path / "docs" / "decisions" / "001-auth.md").is_file()
    assert not (tmp_path / "docs" / "decisions" / "001-auth.grill.md").exists()


@pytest.mark.asyncio
async def test_domain_adr_approval_revise_does_not_promote(tmp_path: Path):
    from agent.lifecycle.scaffold import domain_adr_approval

    _seed_domain_workspace(tmp_path, with_grill=True)
    task = _make_scaffold_task(status="awaiting_domain_adr_approval")

    with (
        patch.object(domain_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(domain_adr_approval, "transition", AsyncMock()),
        patch.object(
            domain_adr_approval,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
    ):
        await domain_adr_approval.apply_verdict(
            task.id, "auth", {"verdict": "revise", "comments": "tighten scope"}
        )

    assert not (tmp_path / "docs" / "decisions" / "001-auth.md").exists()
    assert not (tmp_path / "docs" / "decisions" / "001-auth.grill.md").exists()


@pytest.mark.asyncio
async def test_domain_adr_approval_revise_exhausted_does_not_promote(tmp_path: Path):
    """4th revise auto-rejects (effective='rejected') → must not promote."""

    import json

    from agent.lifecycle.scaffold import domain_adr_approval
    from agent.lifecycle.workspace_paths import domain_adr_approval_path

    _seed_domain_workspace(tmp_path, with_grill=True)

    # Pre-seed the per-slug verdict file with revise_count=3 so the next
    # revise call hits the cap and is converted to 'rejected'.
    existing = tmp_path / domain_adr_approval_path("auth")
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "slug": "auth",
                "verdict": "revise",
                "comments": "round 3",
                "revise_count": 3,
            }
        )
    )

    task = _make_scaffold_task(status="awaiting_domain_adr_approval")
    with (
        patch.object(domain_adr_approval, "async_session", _patched_session_factory(task)),
        patch.object(domain_adr_approval, "transition", AsyncMock()),
        patch.object(
            domain_adr_approval,
            "prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
    ):
        await domain_adr_approval.apply_verdict(
            task.id, "auth", {"verdict": "revise", "comments": "still wrong"}
        )

    assert not (tmp_path / "docs" / "decisions" / "001-auth.md").exists()
