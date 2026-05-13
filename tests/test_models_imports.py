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
