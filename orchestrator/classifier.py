"""Task complexity classifier — simple rule-based heuristics.

No LLM needed. Claude Code itself decides how to approach the work
once it receives the task.
"""

from __future__ import annotations

import logging
import re

from shared.models import TaskComplexity
from shared.types import ClassificationResult, RiskLevel

log = logging.getLogger(__name__)

# Keywords that suggest a complex task
COMPLEX_KEYWORDS = {
    "redesign", "rewrite", "refactor", "migrate", "architecture",
    "overhaul", "rebuild", "rearchitect", "new feature", "schema change",
    "database migration", "auth", "authentication", "authorization",
    "performance", "optimize", "security", "breaking change",
}

# Keywords that suggest a simple task
SIMPLE_KEYWORDS = {
    "typo", "copy", "text change", "rename", "bump", "update version",
    "config", "env", "readme", "comment", "log", "lint", "format",
}


def classify_task(title: str, description: str) -> tuple[TaskComplexity, ClassificationResult]:
    """Classify a task as simple or complex using keyword heuristics."""
    text = f"{title} {description}".lower()

    # Check for explicit complexity signals
    for kw in COMPLEX_KEYWORDS:
        if kw in text:
            result = ClassificationResult(
                classification="complex",
                reasoning=f"Matched complex keyword: '{kw}'",
                estimated_files=5,
                risk=RiskLevel.MEDIUM,
            )
            log.info(f"Classified as complex: matched '{kw}'")
            return TaskComplexity.COMPLEX, result

    for kw in SIMPLE_KEYWORDS:
        if kw in text:
            result = ClassificationResult(
                classification="simple",
                reasoning=f"Matched simple keyword: '{kw}'",
                estimated_files=1,
                risk=RiskLevel.LOW,
            )
            log.info(f"Classified as simple: matched '{kw}'")
            return TaskComplexity.SIMPLE, result

    # Default: if description is short, probably simple
    word_count = len(text.split())
    if word_count < 30:
        result = ClassificationResult(
            classification="simple",
            reasoning="Short description, defaulting to simple",
            estimated_files=2,
            risk=RiskLevel.LOW,
        )
        log.info("Classified as simple: short description")
        return TaskComplexity.SIMPLE, result

    # Longer descriptions default to complex
    result = ClassificationResult(
        classification="complex",
        reasoning="Detailed description suggests non-trivial scope",
        estimated_files=4,
        risk=RiskLevel.MEDIUM,
    )
    log.info("Classified as complex: long description")
    return TaskComplexity.COMPLEX, result
