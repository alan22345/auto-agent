"""Tests for orchestrator/classifier.py — complexity classification."""

from orchestrator.classifier import classify_task
from shared.models import TaskComplexity


def test_copy_deck_with_research_keyword_is_not_no_code():
    """Regression: a 'create a page' task with marketing copy containing the word
    'research' used to trip the NO_CODE regex and route through the query handler.
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


def test_query_title_is_no_code():
    """A genuine question-style title still classifies as SIMPLE_NO_CODE."""
    complexity, _ = classify_task(
        "What is the cheapest way to host a Next.js app?",
        "Considering Vercel, Cloudflare Pages, and Netlify.",
    )
    assert complexity == TaskComplexity.SIMPLE_NO_CODE


def test_compare_title_is_no_code():
    complexity, _ = classify_task(
        "Compare Postgres vs MySQL for our workload",
        "We have mostly read-heavy traffic.",
    )
    assert complexity == TaskComplexity.SIMPLE_NO_CODE


def test_no_code_keyword_only_in_description_is_not_no_code():
    """Words like 'research', 'explain', 'compare' appearing only in the body
    (e.g. quoted copy) must not force a query classification."""
    complexity, _ = classify_task(
        "Build admin dashboard",
        "The dashboard should let users research trends and compare metrics.",
    )
    assert complexity != TaskComplexity.SIMPLE_NO_CODE
