"""Verify the models package preserves all previously-public imports.

Every name that callers can `from shared.models import X` today must still work
after the split. This test pins backward-compat.
"""


def test_existing_public_names_importable():
    from shared.models import (
        Base,
        Task, TaskStatus, TaskComplexity, TaskSource,
        Repo, Plan,
        Organization, OrganizationMembership, User,
        FreeformConfig, Suggestion, SuggestionStatus,
        VerifyAttempt, ReviewAttempt,
        MarketBrief,
    )
    assert Base is not None
    assert TaskStatus.INTAKE.value == "intake"


def test_new_trio_names_importable():
    from shared.models import (
        TaskStatus,
        TrioPhase,
        ArchitectAttempt,
        TrioReviewAttempt,
    )
    assert TaskStatus.TRIO_EXECUTING.value == "trio_executing"
    assert TaskStatus.TRIO_REVIEW.value == "trio_review"
    assert TrioPhase.ARCHITECTING.value == "architecting"
    assert ArchitectAttempt.__tablename__ == "architect_attempts"
    assert TrioReviewAttempt.__tablename__ == "trio_review_attempts"


def test_task_has_trio_columns():
    from shared.models import Task
    assert "parent_task_id" in Task.__table__.columns
    assert "trio_phase" in Task.__table__.columns
    assert "trio_backlog" in Task.__table__.columns
    assert "consulting_architect" in Task.__table__.columns
