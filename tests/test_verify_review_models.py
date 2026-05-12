import uuid

from shared.models import (
    FreeformConfig,
    Organization,
    Plan,
    Repo,
    ReviewAttempt,
    Task,
    TaskSource,
    TaskStatus,
    VerifyAttempt,
)


async def _seed_org(session) -> Organization:
    plan = Plan(
        name=f"plan-{uuid.uuid4().hex[:6]}",
        max_concurrent_tasks=2,
        max_tasks_per_day=100,
        max_input_tokens_per_day=10_000_000,
        max_output_tokens_per_day=2_500_000,
        max_members=5,
    )
    session.add(plan)
    await session.flush()
    org = Organization(
        name=f"org-{uuid.uuid4().hex[:6]}",
        slug=f"org-{uuid.uuid4().hex[:8]}",
        plan_id=plan.id,
    )
    session.add(org)
    await session.flush()
    return org


async def _seed_repo(session, org: Organization) -> Repo:
    repo = Repo(
        name=f"repo-{uuid.uuid4().hex[:6]}",
        url="https://github.com/test/repo",
        organization_id=org.id,
    )
    session.add(repo)
    await session.flush()
    return repo


async def test_task_has_affected_routes_default(session):
    org = await _seed_org(session)
    t = Task(title="t", source=TaskSource.MANUAL, status=TaskStatus.INTAKE, organization_id=org.id)
    session.add(t)
    await session.flush()
    assert t.affected_routes == []


async def test_verify_attempt_roundtrip(session):
    org = await _seed_org(session)
    t = Task(title="t", source=TaskSource.MANUAL, status=TaskStatus.VERIFYING, organization_id=org.id)
    session.add(t)
    await session.flush()
    a = VerifyAttempt(
        task_id=t.id, cycle=1, status="pass",
        boot_check="pass", intent_check="pass",
        intent_judgment="looks good", tool_calls=[],
    )
    session.add(a)
    await session.flush()
    assert a.id is not None


async def test_review_attempt_roundtrip(session):
    org = await _seed_org(session)
    t = Task(title="t", source=TaskSource.MANUAL, status=TaskStatus.AWAITING_REVIEW, organization_id=org.id)
    session.add(t)
    await session.flush()
    a = ReviewAttempt(
        task_id=t.id, cycle=1, status="pass",
        code_review_verdict="OK", ui_check="skipped",
    )
    session.add(a)
    await session.flush()
    assert a.id is not None


def test_freeform_config_run_command_nullable():
    """FreeformConfig.run_command column is declared and defaults to None (no DB required)."""
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(FreeformConfig)
    col_names = [c.key for c in mapper.mapper.column_attrs]
    assert "run_command" in col_names
    # Check the column itself is nullable
    col = mapper.mapper.columns["run_command"]
    assert col.nullable is True
