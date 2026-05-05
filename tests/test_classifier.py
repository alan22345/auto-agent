"""Tests for orchestrator/classifier.py — complexity classification."""

from orchestrator.classifier import classify_task
from shared.models import TaskComplexity


def test_no_task_classifies_as_simple_no_code():
    """The SIMPLE_NO_CODE path was removed — keyword-matching titles like
    'research' or 'compare' was misrouting real coding tasks (e.g. landing
    pages with marketing copy) through the query handler. Every input now
    goes through the coding pipeline.
    """
    inputs = [
        ("What is the cheapest way to host a Next.js app?", "Considering Vercel."),
        ("Compare Postgres vs MySQL for our workload", "Mostly read-heavy traffic."),
        ("Research best auth providers", "Need passkeys."),
        ("Explain how the scheduler works", "I'm onboarding."),
        ("Cardamon Website: Research & Discovery Automation Alternative",
         "Build a landing page targeting users who automate research."),
    ]
    for title, description in inputs:
        complexity, _ = classify_task(title, description)
        assert complexity != TaskComplexity.SIMPLE_NO_CODE, (
            f"{title!r} classified as SIMPLE_NO_CODE — that bucket was removed"
        )


def test_copy_deck_with_research_keyword_is_not_no_code():
    """Regression: a 'create a page' task with marketing copy containing the
    word 'research' used to trip the NO_CODE regex and route through the
    query handler. Now trivially true since SIMPLE_NO_CODE is gone, but kept
    as a guard against any future re-introduction.
    """
    title = "Cardamon Website Dispatch Page"
    description = (
        "cardamon repo - create new page \"Dispatch\"\n\n"
        "Entry 001: Welcome to Dispatch\n"
        "Cardamon replaces recurring reports, inbox sorting, news research, "
        "status updates; the kind of tasks that drain your team."
    )
    complexity, _ = classify_task(title, description)
    assert complexity != TaskComplexity.SIMPLE_NO_CODE


def test_classifier_returns_one_of_three_active_buckets():
    """The classifier should only emit SIMPLE, COMPLEX, or COMPLEX_LARGE."""
    active = {TaskComplexity.SIMPLE, TaskComplexity.COMPLEX, TaskComplexity.COMPLEX_LARGE}
    samples = [
        ("Bump Next.js to 14.2", "Just a version bump."),
        ("Add Stripe billing", "Add subscription tiers and webhook handling for invoices."),
        ("Refactor authentication and authorization across the API",
         "We need to migrate the auth middleware, update all 47 routes, "
         "rebuild the session token storage, add rate limiting, and ensure "
         "the new system is backward compatible with existing tokens."),
    ]
    for title, description in samples:
        complexity, _ = classify_task(title, description)
        assert complexity in active, f"{title!r} → {complexity}"
