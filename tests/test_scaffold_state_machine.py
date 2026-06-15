"""ADR-018 Stage 1 — SCAFFOLD complexity + scaffold parent state machine.

Verifies the new ``TaskComplexity.SCAFFOLD`` enum value, the 8 new
``TaskStatus`` values, and that ``orchestrator.state_machine.TRANSITIONS``
allows the legal moves and rejects illegal ones.

Complexity gating (e.g. INTAKE → AWAITING_INTENT_GRILL only for SCAFFOLD
tasks) is enforced at the caller in later stages, not in the transition
dict — these tests only assert structural reachability.
"""

from __future__ import annotations

import os
import uuid

import pytest

from orchestrator.state_machine import (
    TRANSITIONS,
    InvalidTransition,
    transition,
)
from shared.models import TaskComplexity, TaskStatus

# ---------------------------------------------------------------------------
# Enum surface
# ---------------------------------------------------------------------------


def test_scaffold_complexity_exists() -> None:
    assert TaskComplexity.SCAFFOLD.value == "scaffold"


def test_all_eight_new_statuses_exist() -> None:
    expected = {
        "awaiting_intent_grill",
        "building_root_adr",
        "awaiting_root_adr_approval",
        "building_domain_adrs",
        "awaiting_domain_adr_approval",
        "dispatching_domain_builds",
        "building_domains",
        "awaiting_final_verification",
    }
    actual = {s.value for s in TaskStatus}
    missing = expected - actual
    assert not missing, f"missing scaffold statuses: {missing}"


def test_stage_8_awaiting_domain_grill_status_exists() -> None:
    """ADR-018 Stage 8 — per-domain grill round adds a new wait-state."""
    assert TaskStatus.AWAITING_DOMAIN_GRILL.value == "awaiting_domain_grill"


# ---------------------------------------------------------------------------
# Entry: INTAKE / CLASSIFYING → AWAITING_INTENT_GRILL
# ---------------------------------------------------------------------------


def test_intake_can_enter_awaiting_intent_grill() -> None:
    assert TaskStatus.AWAITING_INTENT_GRILL in TRANSITIONS[TaskStatus.INTAKE]


def test_classifying_can_enter_awaiting_intent_grill() -> None:
    assert TaskStatus.AWAITING_INTENT_GRILL in TRANSITIONS[TaskStatus.CLASSIFYING]


# ---------------------------------------------------------------------------
# Root ADR phase
# ---------------------------------------------------------------------------


def test_awaiting_intent_grill_to_building_root_adr() -> None:
    assert TRANSITIONS[TaskStatus.AWAITING_INTENT_GRILL] == {
        TaskStatus.BUILDING_ROOT_ADR,
    }


def test_building_root_adr_to_awaiting_root_adr_approval() -> None:
    assert TRANSITIONS[TaskStatus.BUILDING_ROOT_ADR] == {
        TaskStatus.AWAITING_ROOT_ADR_APPROVAL,
    }


def test_awaiting_root_adr_approval_branches() -> None:
    allowed = TRANSITIONS[TaskStatus.AWAITING_ROOT_ADR_APPROVAL]
    # approved → next phase
    assert TaskStatus.BUILDING_DOMAIN_ADRS in allowed
    # revise → resume architect's session
    assert TaskStatus.BUILDING_ROOT_ADR in allowed
    # rejected OR 3 revise rounds exhausted
    assert TaskStatus.BLOCKED in allowed


# ---------------------------------------------------------------------------
# Per-domain ADR phase
# ---------------------------------------------------------------------------


def test_building_domain_adrs_branches_to_grill_or_approval() -> None:
    """ADR-018 Stage 8 — the per-domain grill round can park the parent
    in ``AWAITING_DOMAIN_GRILL`` mid-loop or advance straight to the
    per-domain approval gate when every grill is already complete."""
    allowed = TRANSITIONS[TaskStatus.BUILDING_DOMAIN_ADRS]
    assert TaskStatus.AWAITING_DOMAIN_GRILL in allowed  # grill paused
    assert TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL in allowed  # all done


def test_awaiting_domain_grill_branches() -> None:
    """User answers → resume; rare escalation → blocked."""
    allowed = TRANSITIONS[TaskStatus.AWAITING_DOMAIN_GRILL]
    assert TaskStatus.BUILDING_DOMAIN_ADRS in allowed  # user answered
    assert TaskStatus.BLOCKED in allowed  # escalation


def test_awaiting_domain_grill_cannot_skip_to_approval_gate() -> None:
    """The grill agent's resume MUST go through BUILDING_DOMAIN_ADRS so the
    domain loop re-enters cleanly — direct jump would corrupt progress."""
    assert (
        TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL
        not in TRANSITIONS[TaskStatus.AWAITING_DOMAIN_GRILL]
    )


def test_awaiting_domain_adr_approval_branches() -> None:
    allowed = TRANSITIONS[TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL]
    # any domain marked revise → loop back
    assert TaskStatus.BUILDING_DOMAIN_ADRS in allowed
    # all approved/rejected → dispatch
    assert TaskStatus.DISPATCHING_DOMAIN_BUILDS in allowed


# ---------------------------------------------------------------------------
# Per-domain build phase
# ---------------------------------------------------------------------------


def test_dispatching_domain_builds_to_building_domains() -> None:
    assert TRANSITIONS[TaskStatus.DISPATCHING_DOMAIN_BUILDS] == {
        TaskStatus.BUILDING_DOMAINS,
        # Zero-child dispatch re-enters final verification instead of
        # deadlocking in BUILDING_DOMAINS (scaffold #329).
        TaskStatus.AWAITING_FINAL_VERIFICATION,
    }


def test_building_domains_to_awaiting_final_verification() -> None:
    assert TRANSITIONS[TaskStatus.BUILDING_DOMAINS] == {
        TaskStatus.AWAITING_FINAL_VERIFICATION,
    }


# ---------------------------------------------------------------------------
# Final verification phase
# ---------------------------------------------------------------------------


def test_awaiting_final_verification_branches() -> None:
    allowed = TRANSITIONS[TaskStatus.AWAITING_FINAL_VERIFICATION]
    # verify passed → terminal
    assert TaskStatus.DONE in allowed
    # gaps_found → spawn fix children
    assert TaskStatus.DISPATCHING_DOMAIN_BUILDS in allowed
    # 3 verify rounds exhausted
    assert TaskStatus.BLOCKED in allowed


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


def test_awaiting_intent_grill_cannot_jump_to_done() -> None:
    assert TaskStatus.DONE not in TRANSITIONS[TaskStatus.AWAITING_INTENT_GRILL]


def test_awaiting_intent_grill_cannot_jump_to_dispatch() -> None:
    assert TaskStatus.DISPATCHING_DOMAIN_BUILDS not in TRANSITIONS[TaskStatus.AWAITING_INTENT_GRILL]


def test_building_root_adr_cannot_jump_to_done() -> None:
    assert TaskStatus.DONE not in TRANSITIONS[TaskStatus.BUILDING_ROOT_ADR]


# ---------------------------------------------------------------------------
# Existing transitions remain intact (regression guard)
# ---------------------------------------------------------------------------


def test_classifying_still_reaches_queued() -> None:
    assert TaskStatus.QUEUED in TRANSITIONS[TaskStatus.CLASSIFYING]


def test_intake_still_reaches_classifying() -> None:
    assert TaskStatus.CLASSIFYING in TRANSITIONS[TaskStatus.INTAKE]


# ---------------------------------------------------------------------------
# DB-backed: a SCAFFOLD task can be created in AWAITING_INTENT_GRILL,
# and invalid transitions raise InvalidTransition. Skipped when there's
# no DATABASE_URL (mirrors the rest of the unit suite).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_scaffold_task_can_be_created_in_awaiting_intent_grill(session):
    from shared.models import Organization, Task, TaskSource
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"s-{suffix}", slug=f"s-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    task = Task(
        title="scaffold-task",
        description="build me a thing",
        source=TaskSource.FREEFORM,
        status=TaskStatus.AWAITING_INTENT_GRILL,
        complexity=TaskComplexity.SCAFFOLD,
        organization_id=org.id,
    )
    session.add(task)
    await session.flush()

    assert task.id is not None
    assert task.status == TaskStatus.AWAITING_INTENT_GRILL
    assert task.complexity == TaskComplexity.SCAFFOLD


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_awaiting_intent_grill_to_done_raises_invalid_transition(session):
    from shared.models import Organization, Task, TaskSource
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"s-{suffix}", slug=f"s-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    task = Task(
        title="scaffold-task",
        description="build me a thing",
        source=TaskSource.FREEFORM,
        status=TaskStatus.AWAITING_INTENT_GRILL,
        complexity=TaskComplexity.SCAFFOLD,
        organization_id=org.id,
    )
    session.add(task)
    await session.flush()

    with pytest.raises(InvalidTransition):
        await transition(session, task, TaskStatus.DONE)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_full_scaffold_happy_path_transitions(session):
    """Walk a fake scaffold task through the entire happy-path chain to
    confirm the transition dict composes cleanly end-to-end."""
    from shared.models import Organization, Task, TaskSource
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"s-{suffix}", slug=f"s-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    task = Task(
        title="scaffold-task",
        description="build me a thing",
        source=TaskSource.FREEFORM,
        status=TaskStatus.AWAITING_INTENT_GRILL,
        complexity=TaskComplexity.SCAFFOLD,
        organization_id=org.id,
    )
    session.add(task)
    await session.flush()

    chain = [
        TaskStatus.BUILDING_ROOT_ADR,
        TaskStatus.AWAITING_ROOT_ADR_APPROVAL,
        TaskStatus.BUILDING_DOMAIN_ADRS,
        TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL,
        TaskStatus.DISPATCHING_DOMAIN_BUILDS,
        TaskStatus.BUILDING_DOMAINS,
        TaskStatus.AWAITING_FINAL_VERIFICATION,
        TaskStatus.DONE,
    ]
    for to in chain:
        await transition(session, task, to)
        assert task.status == to
