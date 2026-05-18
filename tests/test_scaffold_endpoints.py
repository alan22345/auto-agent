"""Tests for the ADR-018 Stage 5 scaffold HTTP endpoints.

The router exposes three POSTs that drive the SCAFFOLD parent state
machine (intent grill answer, root ADR verdict, domain ADR verdict)
plus three GETs that surface the markdown artefacts (intent.md, root
ADR, per-domain ADR list). All endpoints reuse ``approve-plan``'s
auth/scoping pattern via ``_get_task_in_org`` and publish a
``standin.decision`` event matching the existing taxonomy.

These tests mock the workspace path so each test writes its fixture
files under ``tmp_path`` and asserts on:
- the JSON / markdown the endpoint persists or returns,
- the lifecycle delegate it invokes,
- the audit event publish.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import (
    get_scaffold_domain_grill_question,
    get_scaffold_intent,
    get_scaffold_root_adr,
    list_scaffold_domain_adrs,
    scaffold_domain_adr_verdict,
    scaffold_domain_grill_answer,
    scaffold_intent_grill_answer,
    scaffold_root_adr_verdict,
)
from shared.models import Task, TaskComplexity, TaskStatus
from shared.types import (
    ScaffoldDomainAdrVerdictRequest,
    ScaffoldDomainGrillAnswerRequest,
    ScaffoldIntentGrillAnswerRequest,
    ScaffoldRootAdrVerdictRequest,
)


def _mock_scaffold_task(
    *,
    task_id: int = 1,
    status: TaskStatus = TaskStatus.AWAITING_INTENT_GRILL,
    organization_id: int = 7,
    complexity: TaskComplexity = TaskComplexity.SCAFFOLD,
):
    t = MagicMock(spec=Task)
    t.id = task_id
    t.status = status
    t.organization_id = organization_id
    t.complexity = complexity
    t.mode_override = None
    return t


# ---------------------------------------------------------------------------
# POST /tasks/{id}/scaffold/intent-grill-answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intent_grill_answer_writes_file_and_dispatches_driver(
    tmp_path, monkeypatch
):
    """Happy path — answer lands at intent_grill_answer.json + driver kicked."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    workspace = tmp_path / "7" / "task-1"
    workspace.mkdir(parents=True)

    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task()

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
        patch(
            "orchestrator.router._dispatch_scaffold_driver"
        ) as dispatch_mock,
    ):
        await scaffold_intent_grill_answer(
            task_id=1,
            req=ScaffoldIntentGrillAnswerRequest(answer="auth via OAuth"),
            session=session,
            org_id=7,
        )

    answer_path = workspace / ".auto-agent" / "intent_grill_answer.json"
    assert answer_path.exists(), "must write intent_grill_answer.json"
    payload = json.loads(answer_path.read_text())
    assert payload["answer"] == "auth via OAuth"
    assert payload["source"] == "user"
    assert payload["schema_version"] == "1"
    dispatch_mock.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_intent_grill_answer_400_when_wrong_status():
    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task(status=TaskStatus.BUILDING_ROOT_ADR)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await scaffold_intent_grill_answer(
            task_id=1,
            req=ScaffoldIntentGrillAnswerRequest(answer="hi"),
            session=session,
            org_id=7,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_intent_grill_answer_400_when_not_scaffold_complexity():
    session = AsyncMock(spec=AsyncSession)
    # COMPLEX_LARGE task happens to be in AWAITING_INTENT_GRILL by some
    # bizarre wiring — the endpoint must still reject it.
    task = _mock_scaffold_task(complexity=TaskComplexity.COMPLEX_LARGE)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await scaffold_intent_grill_answer(
            task_id=1,
            req=ScaffoldIntentGrillAnswerRequest(answer="hi"),
            session=session,
            org_id=7,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_intent_grill_answer_404_when_task_missing():
    session = AsyncMock(spec=AsyncSession)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=None),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await scaffold_intent_grill_answer(
            task_id=999,
            req=ScaffoldIntentGrillAnswerRequest(answer="hi"),
            session=session,
            org_id=7,
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# POST /tasks/{id}/scaffold/root-adr-verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_adr_verdict_delegates_and_dispatches(tmp_path, monkeypatch):
    """Approved verdict → apply_verdict called, driver re-invoked, audit row written."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.refresh = AsyncMock()
    task = _mock_scaffold_task(status=TaskStatus.AWAITING_ROOT_ADR_APPROVAL)

    apply_mock = AsyncMock(return_value=TaskStatus.BUILDING_DOMAIN_ADRS)

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch(
            "agent.lifecycle.scaffold.root_adr_approval.apply_verdict",
            apply_mock,
        ),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
        patch(
            "orchestrator.router._dispatch_scaffold_driver"
        ) as dispatch_mock,
    ):
        await scaffold_root_adr_verdict(
            task_id=1,
            req=ScaffoldRootAdrVerdictRequest(
                verdict="approved", comments="LGTM"
            ),
            session=session,
            org_id=7,
        )

    apply_mock.assert_awaited_once_with(
        1, {"verdict": "approved", "comments": "LGTM"}
    )
    dispatch_mock.assert_called_once_with(1)
    # GateDecision audit row added.
    assert session.add.call_count >= 1


@pytest.mark.asyncio
async def test_root_adr_verdict_rejected_does_not_dispatch_driver(tmp_path, monkeypatch):
    """Rejected verdict parks at BLOCKED; the driver should not be re-kicked."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.refresh = AsyncMock()
    task = _mock_scaffold_task(status=TaskStatus.AWAITING_ROOT_ADR_APPROVAL)

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch(
            "agent.lifecycle.scaffold.root_adr_approval.apply_verdict",
            AsyncMock(return_value=TaskStatus.BLOCKED),
        ),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
        patch(
            "orchestrator.router._dispatch_scaffold_driver"
        ) as dispatch_mock,
    ):
        await scaffold_root_adr_verdict(
            task_id=1,
            req=ScaffoldRootAdrVerdictRequest(
                verdict="rejected", comments="no thanks"
            ),
            session=session,
            org_id=7,
        )

    dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_root_adr_verdict_400_when_wrong_status():
    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task(status=TaskStatus.BUILDING_ROOT_ADR)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await scaffold_root_adr_verdict(
            task_id=1,
            req=ScaffoldRootAdrVerdictRequest(verdict="approved"),
            session=session,
            org_id=7,
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# POST /tasks/{id}/scaffold/domain-adr-verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_adr_verdict_dispatches_when_state_advances(
    tmp_path, monkeypatch
):
    """When apply_verdict returns DISPATCHING_DOMAIN_BUILDS, the driver fires."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.refresh = AsyncMock()
    task = _mock_scaffold_task(status=TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL)

    apply_mock = AsyncMock(return_value=TaskStatus.DISPATCHING_DOMAIN_BUILDS)

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch(
            "agent.lifecycle.scaffold.domain_adr_approval.apply_verdict",
            apply_mock,
        ),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
        patch(
            "orchestrator.router._dispatch_scaffold_driver"
        ) as dispatch_mock,
    ):
        await scaffold_domain_adr_verdict(
            task_id=1,
            req=ScaffoldDomainAdrVerdictRequest(
                domain_slug="auth", verdict="approved"
            ),
            session=session,
            org_id=7,
        )

    apply_mock.assert_awaited_once_with(
        1, "auth", {"verdict": "approved", "comments": ""}
    )
    dispatch_mock.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_domain_adr_verdict_no_dispatch_when_still_waiting(
    tmp_path, monkeypatch
):
    """A partial-fan-in verdict (some domains still on revise) holds state."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.refresh = AsyncMock()
    task = _mock_scaffold_task(status=TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL)

    apply_mock = AsyncMock(return_value=TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL)

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch(
            "agent.lifecycle.scaffold.domain_adr_approval.apply_verdict",
            apply_mock,
        ),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
        patch(
            "orchestrator.router._dispatch_scaffold_driver"
        ) as dispatch_mock,
    ):
        await scaffold_domain_adr_verdict(
            task_id=1,
            req=ScaffoldDomainAdrVerdictRequest(
                domain_slug="billing", verdict="approved"
            ),
            session=session,
            org_id=7,
        )

    # No state advance → no driver kick.
    dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_domain_adr_verdict_400_when_wrong_status():
    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task(status=TaskStatus.BUILDING_DOMAIN_ADRS)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await scaffold_domain_adr_verdict(
            task_id=1,
            req=ScaffoldDomainAdrVerdictRequest(
                domain_slug="auth", verdict="approved"
            ),
            session=session,
            org_id=7,
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# GET /tasks/{id}/scaffold/intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scaffold_intent_returns_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    workspace = tmp_path / "7" / "task-1"
    (workspace / ".auto-agent").mkdir(parents=True)
    (workspace / ".auto-agent" / "intent.md").write_text("# Intent\nBuild a thing.")

    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task()

    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=task),
    ):
        out = await get_scaffold_intent(task_id=1, session=session, org_id=7)
    assert "Build a thing" in out.markdown


@pytest.mark.asyncio
async def test_get_scaffold_intent_404_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task()
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await get_scaffold_intent(task_id=1, session=session, org_id=7)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# GET /tasks/{id}/scaffold/root-adr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scaffold_root_adr_returns_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    workspace = tmp_path / "7" / "task-1"
    adrs = workspace / ".auto-agent" / "adrs"
    adrs.mkdir(parents=True)
    (adrs / "000-system.md").write_text("# System ADR\n## Vision\nBe great.\n")

    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task()
    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=task),
    ):
        out = await get_scaffold_root_adr(task_id=1, session=session, org_id=7)
    assert "Vision" in out.markdown


@pytest.mark.asyncio
async def test_get_scaffold_root_adr_404_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task()
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await get_scaffold_root_adr(task_id=1, session=session, org_id=7)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# GET /tasks/{id}/scaffold/domain-adrs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_domain_adrs_skips_root_and_attaches_approvals(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    workspace = tmp_path / "7" / "task-1"
    adrs = workspace / ".auto-agent" / "adrs"
    adrs.mkdir(parents=True)
    approvals = workspace / ".auto-agent" / "domain_adr_approvals"
    approvals.mkdir(parents=True)

    (adrs / "000-system.md").write_text("# Root\n")
    (adrs / "001-auth.md").write_text("# Auth domain\n## Scope\n")
    (adrs / "002-billing.md").write_text("# Billing domain\n## Scope\n")
    # A non-matching file should be ignored.
    (adrs / "notes.md").write_text("scratch")

    (approvals / "auth.json").write_text(
        json.dumps({"slug": "auth", "verdict": "approved", "comments": "ok"})
    )

    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task()
    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=task),
    ):
        entries = await list_scaffold_domain_adrs(
            task_id=1, session=session, org_id=7
        )

    slugs = sorted(e.slug for e in entries)
    assert slugs == ["auth", "billing"], "root ADR + non-matching files excluded"
    by_slug = {e.slug: e for e in entries}
    assert by_slug["auth"].index == 1
    assert by_slug["auth"].name == "Auth domain"
    assert by_slug["auth"].approval == {
        "verdict": "approved",
        "comments": "ok",
        "revise_count": 0,
    }
    assert by_slug["billing"].approval is None  # no verdict file yet


# ---------------------------------------------------------------------------
# ADR-018 Stage 8 — POST /tasks/{id}/scaffold/domain-grill-answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_grill_answer_writes_file_and_dispatches_driver(
    tmp_path, monkeypatch
):
    """Happy path — answer lands at domain_grill_answers/<slug>.json + driver kicked."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    workspace = tmp_path / "7" / "task-1"
    workspace.mkdir(parents=True)

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    task = _mock_scaffold_task(status=TaskStatus.AWAITING_DOMAIN_GRILL)

    transition_mock = AsyncMock()

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
        patch(
            "orchestrator.state_machine.transition",
            transition_mock,
        ),
        patch(
            "orchestrator.router._dispatch_scaffold_driver"
        ) as dispatch_mock,
    ):
        await scaffold_domain_grill_answer(
            task_id=1,
            req=ScaffoldDomainGrillAnswerRequest(
                domain_slug="auth", answer="store sessions per user"
            ),
            session=session,
            org_id=7,
        )

    answer_path = workspace / ".auto-agent" / "domain_grill_answers" / "auth.json"
    assert answer_path.exists(), "must write domain_grill_answers/auth.json"
    payload = json.loads(answer_path.read_text())
    assert payload["domain_slug"] == "auth"
    assert payload["answer"] == "store sessions per user"
    assert payload["source"] == "user"
    assert payload["schema_version"] == "1"
    # State machine transition AWAITING_DOMAIN_GRILL → BUILDING_DOMAIN_ADRS.
    transition_mock.assert_awaited_once()
    transition_args = transition_mock.await_args.args
    assert transition_args[2] == TaskStatus.BUILDING_DOMAIN_ADRS
    dispatch_mock.assert_called_once_with(1)
    # GateDecision audit row added (the only session.add in this path).
    assert session.add.call_count >= 1


@pytest.mark.asyncio
async def test_domain_grill_answer_400_when_wrong_status():
    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task(status=TaskStatus.BUILDING_DOMAIN_ADRS)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await scaffold_domain_grill_answer(
            task_id=1,
            req=ScaffoldDomainGrillAnswerRequest(domain_slug="auth", answer="hi"),
            session=session,
            org_id=7,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_domain_grill_answer_400_when_not_scaffold_complexity():
    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task(
        status=TaskStatus.AWAITING_DOMAIN_GRILL,
        complexity=TaskComplexity.COMPLEX_LARGE,
    )
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await scaffold_domain_grill_answer(
            task_id=1,
            req=ScaffoldDomainGrillAnswerRequest(domain_slug="auth", answer="hi"),
            session=session,
            org_id=7,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_get_domain_grill_question_returns_payload(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    workspace = tmp_path / "7" / "task-1"
    qdir = workspace / ".auto-agent" / "domain_grill_questions"
    qdir.mkdir(parents=True)
    (qdir / "auth.json").write_text(
        json.dumps({
            "schema_version": "1",
            "domain_slug": "auth",
            "question": "Are sessions per user or per device?",
        })
    )

    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task(status=TaskStatus.AWAITING_DOMAIN_GRILL)
    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=task),
    ):
        out = await get_scaffold_domain_grill_question(
            task_id=1, slug="auth", session=session, org_id=7
        )
    assert out.domain_slug == "auth"
    assert "per user" in out.question


@pytest.mark.asyncio
async def test_get_domain_grill_question_404_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task(status=TaskStatus.AWAITING_DOMAIN_GRILL)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await get_scaffold_domain_grill_question(
            task_id=1, slug="auth", session=session, org_id=7
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_domain_adrs_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    session = AsyncMock(spec=AsyncSession)
    task = _mock_scaffold_task()
    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=task),
    ):
        out = await list_scaffold_domain_adrs(
            task_id=1, session=session, org_id=7
        )
    assert out == []
