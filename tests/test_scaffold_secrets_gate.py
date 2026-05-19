"""ADR-019 T7 — AWAITING_REQUIRED_SECRETS gate tests.

Tests cover:
- New status exists and transitions are legal.
- Gate: all domain ADRs done + no missing secrets → DISPATCHING_DOMAIN_BUILDS.
- Gate: all domain ADRs done + missing secrets → AWAITING_REQUIRED_SECRETS.
- PUT hook: last missing secret triggers run_scaffold_parent re-invocation.
- PUT hook: setting one of two missing secrets does NOT re-dispatch.
- PUT hook: no parked scaffold parent → no-op.
- Recheck endpoint happy path: all secrets set → 200 unblocked=true.
- Recheck endpoint still missing → 200 unblocked=false.
- Recheck endpoint non-parked task → 409.
- Recheck endpoint missing task → 404.
- Recheck endpoint cross-org → 403.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.state_machine import TRANSITIONS
from shared.models import Task, TaskComplexity, TaskStatus

# ---------------------------------------------------------------------------
# Status and transition map
# ---------------------------------------------------------------------------


def test_awaiting_required_secrets_status_exists() -> None:
    assert TaskStatus.AWAITING_REQUIRED_SECRETS.value == "awaiting_required_secrets"


def test_awaiting_domain_adr_approval_can_transition_to_awaiting_required_secrets() -> None:
    assert TaskStatus.AWAITING_REQUIRED_SECRETS in TRANSITIONS[TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL]


def test_awaiting_required_secrets_can_transition_to_dispatching_domain_builds() -> None:
    assert TaskStatus.DISPATCHING_DOMAIN_BUILDS in TRANSITIONS[TaskStatus.AWAITING_REQUIRED_SECRETS]


def test_awaiting_required_secrets_can_transition_to_blocked() -> None:
    assert TaskStatus.BLOCKED in TRANSITIONS[TaskStatus.AWAITING_REQUIRED_SECRETS]


def test_awaiting_required_secrets_cannot_transition_to_done() -> None:
    assert TaskStatus.DONE not in TRANSITIONS.get(TaskStatus.AWAITING_REQUIRED_SECRETS, set())


# ---------------------------------------------------------------------------
# list_missing_architect_required helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_missing_returns_empty_when_no_rows() -> None:
    """No architect_required rows → empty list (gate is green)."""
    from unittest.mock import AsyncMock

    from shared import repo_secrets

    class _StubResult:
        def all(self):
            return []

    sess = AsyncMock(spec=AsyncSession)
    sess.execute = AsyncMock(return_value=_StubResult())
    sess.close = AsyncMock()

    result = await repo_secrets.list_missing_architect_required(
        42, organization_id=7, session=sess
    )
    assert result == []


@pytest.mark.asyncio
async def test_list_missing_returns_keys_with_null_value_enc() -> None:
    """Rows with source='architect_required' AND value_enc IS NULL → returned."""
    from shared import repo_secrets

    class _StubResult:
        def all(self):
            return [("STRIPE_API_KEY",), ("OPENAI_API_KEY",)]

    sess = AsyncMock(spec=AsyncSession)
    sess.execute = AsyncMock(return_value=_StubResult())
    sess.close = AsyncMock()

    result = await repo_secrets.list_missing_architect_required(
        42, organization_id=7, session=sess
    )
    assert result == ["STRIPE_API_KEY", "OPENAI_API_KEY"]


@pytest.mark.asyncio
async def test_list_missing_sql_filters_architect_required_and_null() -> None:
    """Verify the SQL query filters on source AND value_enc IS NULL."""
    from shared import repo_secrets

    class _StubResult:
        def all(self):
            return []

    sess = AsyncMock(spec=AsyncSession)
    sess.execute = AsyncMock(return_value=_StubResult())
    sess.close = AsyncMock()

    await repo_secrets.list_missing_architect_required(1, organization_id=1, session=sess)

    args, _ = sess.execute.await_args
    sql = str(args[0]).lower()
    assert "architect_required" in sql
    assert "null" in sql  # checks IS NULL condition


@pytest.mark.asyncio
async def test_list_missing_caller_session_not_closed() -> None:
    """Caller-supplied session is not closed by the helper."""
    from shared import repo_secrets

    class _StubResult:
        def all(self):
            return []

    sess = AsyncMock(spec=AsyncSession)
    sess.execute = AsyncMock(return_value=_StubResult())
    sess.close = AsyncMock()

    await repo_secrets.list_missing_architect_required(1, organization_id=1, session=sess)
    sess.close.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dispatch-children gate logic
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: int = 1,
    status: TaskStatus = TaskStatus.AWAITING_REQUIRED_SECRETS,
    organization_id: int = 7,
    repo_id: int = 42,
    complexity: TaskComplexity = TaskComplexity.SCAFFOLD,
    subtasks: dict | None = None,
) -> MagicMock:
    t = MagicMock(spec=Task)
    t.id = task_id
    t.status = status
    t.organization_id = organization_id
    t.repo_id = repo_id
    t.complexity = complexity
    t.subtasks = subtasks or {}
    t.freeform_mode = False
    t.source = None
    t.created_by_user_id = None
    return t


@pytest.mark.asyncio
async def test_gate_no_missing_secrets_transitions_to_dispatching(tmp_path, monkeypatch) -> None:
    """When no architect-required secrets are missing, the gate passes and
    the parent transitions to DISPATCHING_DOMAIN_BUILDS."""
    from agent.lifecycle.scaffold import dispatch_children

    task = _make_task()

    with (
        patch(
            "agent.lifecycle.scaffold.dispatch_children.list_missing_architect_required",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agent.lifecycle.scaffold.dispatch_children._transition_parent",
            new=AsyncMock(return_value=True),
        ) as mock_transition,
        patch(
            "agent.lifecycle.scaffold.dispatch_children._persist_missing_secrets",
            new=AsyncMock(),
        ),
    ):
        result = await dispatch_children.check_secrets_gate(task)

    assert result is True  # gate is green
    mock_transition.assert_awaited_once_with(
        task.id, TaskStatus.DISPATCHING_DOMAIN_BUILDS
    )


@pytest.mark.asyncio
async def test_gate_missing_secrets_parks_at_awaiting_required_secrets(
    tmp_path, monkeypatch
) -> None:
    """When architect-required secrets are missing, the parent stays parked
    at AWAITING_REQUIRED_SECRETS and missing keys are persisted."""
    from agent.lifecycle.scaffold import dispatch_children

    task = _make_task()

    with (
        patch(
            "agent.lifecycle.scaffold.dispatch_children.list_missing_architect_required",
            new=AsyncMock(return_value=["STRIPE_API_KEY", "OPENAI_API_KEY"]),
        ),
        patch(
            "agent.lifecycle.scaffold.dispatch_children._transition_parent",
            new=AsyncMock(),
        ) as mock_transition,
        patch(
            "agent.lifecycle.scaffold.dispatch_children._persist_missing_secrets",
            new=AsyncMock(),
        ) as mock_persist,
    ):
        result = await dispatch_children.check_secrets_gate(task)

    assert result is False  # gate is red
    mock_transition.assert_not_awaited()
    mock_persist.assert_awaited_once_with(
        task.id, ["STRIPE_API_KEY", "OPENAI_API_KEY"]
    )


# ---------------------------------------------------------------------------
# PUT hook — re-evaluate gate on secret set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_hook_last_missing_secret_triggers_redispatch() -> None:
    """Setting the last required secret triggers run_scaffold_parent re-invocation."""
    from orchestrator.router import put_repo_secret
    from shared.types import RepoSecretPutRequest

    repo = MagicMock()
    repo.id = 42
    repo.organization_id = 7

    parked_task = _make_task(repo_id=42, organization_id=7)

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()

    with (
        patch("orchestrator.router._check_repo_access", new=AsyncMock(return_value=repo)),
        patch("shared.repo_secrets.set", new=AsyncMock()),
        patch(
            "orchestrator.router._find_parked_scaffold_parents",
            new=AsyncMock(return_value=[parked_task]),
        ),
        patch(
            "orchestrator.router._recheck_secrets_gate_for_task",
            new=AsyncMock(return_value=True),
        ) as mock_recheck,
        patch("orchestrator.router._dispatch_scaffold_driver") as mock_dispatch,
    ):
        result = await put_repo_secret(
            repo_id=42,
            key="STRIPE_API_KEY",
            body=RepoSecretPutRequest(value="sk_live_xxx"),
            session=session,
            org_id=7,
        )

    assert result["ok"] is True
    mock_recheck.assert_awaited_once()
    mock_dispatch.assert_called_once_with(parked_task.id)


@pytest.mark.asyncio
async def test_put_hook_still_missing_no_dispatch() -> None:
    """Setting one of two missing secrets does NOT re-dispatch."""
    from orchestrator.router import put_repo_secret
    from shared.types import RepoSecretPutRequest

    repo = MagicMock()
    repo.id = 42
    repo.organization_id = 7

    parked_task = _make_task(repo_id=42, organization_id=7)

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()

    with (
        patch("orchestrator.router._check_repo_access", new=AsyncMock(return_value=repo)),
        patch("shared.repo_secrets.set", new=AsyncMock()),
        patch(
            "orchestrator.router._find_parked_scaffold_parents",
            new=AsyncMock(return_value=[parked_task]),
        ),
        patch(
            "orchestrator.router._recheck_secrets_gate_for_task",
            new=AsyncMock(return_value=False),
        ),
        patch("orchestrator.router._dispatch_scaffold_driver") as mock_dispatch,
    ):
        result = await put_repo_secret(
            repo_id=42,
            key="STRIPE_API_KEY",
            body=RepoSecretPutRequest(value="sk_live_xxx"),
            session=session,
            org_id=7,
        )

    assert result["ok"] is True
    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_put_hook_no_parked_parent_is_noop() -> None:
    """Setting a secret for a repo with no parked scaffold parent is a no-op."""
    from orchestrator.router import put_repo_secret
    from shared.types import RepoSecretPutRequest

    repo = MagicMock()
    repo.id = 55
    repo.organization_id = 7

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()

    with (
        patch("orchestrator.router._check_repo_access", new=AsyncMock(return_value=repo)),
        patch("shared.repo_secrets.set", new=AsyncMock()),
        patch(
            "orchestrator.router._find_parked_scaffold_parents",
            new=AsyncMock(return_value=[]),
        ),
        patch("orchestrator.router._dispatch_scaffold_driver") as mock_dispatch,
    ):
        result = await put_repo_secret(
            repo_id=55,
            key="MY_KEY",
            body=RepoSecretPutRequest(value="myval"),
            session=session,
            org_id=7,
        )

    assert result["ok"] is True
    mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# POST /scaffold/{task_id}/recheck-secrets endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recheck_secrets_happy_path_all_set() -> None:
    """Happy path: task parked, all secrets now set → 200 unblocked=true, driver fires."""
    from orchestrator.router import scaffold_recheck_secrets

    task = _make_task(status=TaskStatus.AWAITING_REQUIRED_SECRETS)
    session = AsyncMock(spec=AsyncSession)

    with (
        patch("orchestrator.router._get_task_in_org", new=AsyncMock(return_value=task)),
        patch(
            "orchestrator.router._recheck_secrets_gate_for_task",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "orchestrator.router._find_missing_for_task",
            new=AsyncMock(return_value=[]),
        ),
        patch("orchestrator.router._dispatch_scaffold_driver") as mock_dispatch,
    ):
        result = await scaffold_recheck_secrets(
            task_id=1,
            session=session,
            org_id=7,
        )

    assert result["unblocked"] is True
    assert result["missing"] == []
    mock_dispatch.assert_called_once_with(task.id)


@pytest.mark.asyncio
async def test_recheck_secrets_still_missing() -> None:
    """Still missing secrets → 200 unblocked=false, no transition."""
    from orchestrator.router import scaffold_recheck_secrets

    task = _make_task(status=TaskStatus.AWAITING_REQUIRED_SECRETS)
    session = AsyncMock(spec=AsyncSession)

    with (
        patch("orchestrator.router._get_task_in_org", new=AsyncMock(return_value=task)),
        patch(
            "orchestrator.router._recheck_secrets_gate_for_task",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "orchestrator.router._find_missing_for_task",
            new=AsyncMock(return_value=["STRIPE_API_KEY"]),
        ),
        patch("orchestrator.router._dispatch_scaffold_driver") as mock_dispatch,
    ):
        result = await scaffold_recheck_secrets(
            task_id=1,
            session=session,
            org_id=7,
        )

    assert result["unblocked"] is False
    assert result["missing"] == ["STRIPE_API_KEY"]
    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_recheck_secrets_non_parked_returns_409() -> None:
    """Task not at AWAITING_REQUIRED_SECRETS → 409."""
    from orchestrator.router import scaffold_recheck_secrets

    task = _make_task(status=TaskStatus.BUILDING_DOMAINS)
    session = AsyncMock(spec=AsyncSession)

    with (
        patch("orchestrator.router._get_task_in_org", new=AsyncMock(return_value=task)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await scaffold_recheck_secrets(
            task_id=1,
            session=session,
            org_id=7,
        )

    assert exc_info.value.status_code == 409
    assert "building_domains" in exc_info.value.detail


@pytest.mark.asyncio
async def test_recheck_secrets_task_not_found_returns_404() -> None:
    """Task not found → 404."""
    from orchestrator.router import scaffold_recheck_secrets

    session = AsyncMock(spec=AsyncSession)

    with (
        patch("orchestrator.router._get_task_in_org", new=AsyncMock(return_value=None)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await scaffold_recheck_secrets(
            task_id=999,
            session=session,
            org_id=7,
        )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# _transition_parent concurrency safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_hook_concurrent_transition_does_not_500() -> None:
    """Two concurrent PUTs that both pass the gate must not raise — the
    second one finds the task already transitioned and silently no-ops.

    Simulates by calling _transition_parent twice in sequence: the first
    call succeeds (returns True); the second call raises InvalidTransition
    internally but _transition_parent catches it and returns False instead
    of propagating the exception.
    """
    from agent.lifecycle.scaffold import dispatch_children
    from orchestrator.state_machine import InvalidTransition

    call_count = 0

    async def _fake_transition(s, task, to_status, message=""):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise InvalidTransition(
                f"Cannot transition from {to_status.value} to {to_status.value}."
            )
        # First call succeeds — just return the task (matching real signature).
        return task

    task = _make_task()

    with (
        patch(
            "agent.lifecycle.scaffold.dispatch_children.async_session"
        ) as mock_session_cm,
        patch(
            "orchestrator.state_machine.transition",
            new=AsyncMock(side_effect=_fake_transition),
        ),
    ):
        # Set up async context manager for async_session().
        mock_session = AsyncMock()
        scalar_result = MagicMock()
        scalar_result.scalar_one = MagicMock(return_value=task)
        mock_session.execute = AsyncMock(return_value=scalar_result)
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cm.return_value = mock_session

        # First call — transition fires, returns True.
        first = await dispatch_children._transition_parent(
            task.id, TaskStatus.DISPATCHING_DOMAIN_BUILDS
        )
        # Second call — InvalidTransition raised internally, caught, returns False.
        second = await dispatch_children._transition_parent(
            task.id, TaskStatus.DISPATCHING_DOMAIN_BUILDS
        )

    assert first is True, "first transition should fire"
    assert second is False, "second concurrent call should return False, not raise"


@pytest.mark.asyncio
async def test_recheck_secrets_cross_org_returns_403() -> None:
    """Cross-org access → 403 (belt-and-suspenders check fires on org mismatch)."""
    from orchestrator.router import scaffold_recheck_secrets

    # Simulate the scoped helper returning a task whose org doesn't match the
    # caller's org — the same pattern T2's test_list_repo_secrets_403_cross_org
    # uses for per-repo endpoints.
    wrong_org_task = _make_task(organization_id=42)  # org 42, not caller's org 999
    session = AsyncMock(spec=AsyncSession)

    with (
        patch("orchestrator.router._get_task_in_org", new=AsyncMock(return_value=wrong_org_task)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await scaffold_recheck_secrets(
            task_id=1,
            session=session,
            org_id=999,  # wrong org
        )

    assert exc_info.value.status_code == 403
