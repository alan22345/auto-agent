"""LLM-powered task classifier — replaces keyword heuristics with model intelligence.

Falls back to keyword heuristics if the LLM call fails.
"""

from __future__ import annotations

import logging

from agent.llm import get_provider
from agent.llm.structured import complete_json
from agent.llm.types import Message
from shared.types import ClassificationResult

log = logging.getLogger(__name__)


async def classify_task(title: str, description: str) -> ClassificationResult:
    """Classify a task using the LLM, falling back to keyword heuristics."""
    try:
        return await _classify_with_llm(title, description)
    except Exception as e:
        log.warning(f"LLM classification failed, using heuristics: {e}")
        return _classify_with_heuristics(title, description)


async def _classify_with_llm(title: str, description: str) -> ClassificationResult:
    """Use the configured LLM to classify task complexity."""
    provider = get_provider()
    prompt = (
        f"Classify this software task by complexity.\n\n"
        f"Title: {title}\n"
        f"Description: {description[:500]}\n\n"
        f"Output ONLY a JSON object with these fields:\n"
        f'{{"classification": "simple|complex|complex_large", '
        f'"reasoning": "one sentence why", '
        f'"estimated_files": <number>, '
        f'"risk": "low|medium|high"}}\n\n'
        f"Guidelines:\n"
        f"- simple: typo fixes, config changes, copy updates, single-file changes\n"
        f"- complex: new features, bug fixes requiring investigation, multi-file changes\n"
        f"- complex_large: architectural changes, migrations, 5+ files, cross-cutting concerns\n"
    )

    data = await complete_json(
        provider,
        messages=[Message(role="user", content=prompt)],
        system="You are a task classifier. Output only valid JSON.",
        max_tokens=200,
        retries=2,
    )

    classification = data.get("classification", "complex")
    # Map complex_large to complex for the Pydantic type (DB handles the enum separately)
    pydantic_classification = "complex" if classification == "complex_large" else classification
    return ClassificationResult(
        classification=pydantic_classification,
        reasoning=data.get("reasoning", ""),
        estimated_files=data.get("estimated_files", 0),
        risk=data.get("risk", "medium"),
    )


# ---------------------------------------------------------------------------
# Keyword heuristic fallback (from orchestrator/classifier.py)
# ---------------------------------------------------------------------------

_COMPLEX_KEYWORDS = {
    "redesign",
    "refactor",
    "migrate",
    "migration",
    "auth",
    "authentication",
    "authorization",
    "api",
    "endpoint",
    "database",
    "schema",
    "deploy",
    "infrastructure",
    "architecture",
    "security",
    "performance",
    "optimize",
    "integration",
    "webhook",
    "pipeline",
    "ci/cd",
    "testing framework",
}

_SIMPLE_KEYWORDS = {
    "typo",
    "copy",
    "rename",
    "config",
    "configuration",
    "update version",
    "bump",
    "readme",
    "documentation",
    "comment",
    "log",
    "logging",
    "env",
    "environment variable",
    "css",
    "style",
    "color",
    "font",
}


def _classify_with_heuristics(title: str, description: str) -> ClassificationResult:
    """Rule-based fallback classification."""
    text = f"{title} {description}".lower()

    complex_count = sum(1 for kw in _COMPLEX_KEYWORDS if kw in text)
    simple_count = sum(1 for kw in _SIMPLE_KEYWORDS if kw in text)
    word_count = len(text.split())

    if complex_count >= 3 and word_count >= 60:
        classification = "complex"  # complex_large maps to complex in the Pydantic enum
    elif complex_count > simple_count:
        classification = "complex"
    else:
        classification = "simple"

    return ClassificationResult(
        classification=classification,
        reasoning="Classified by keyword heuristics (LLM fallback)",
    )
