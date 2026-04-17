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
    "pricing", "subscription", "stripe", "payment", "billing",
    "admin panel", "dashboard", "anonymise", "anonymize",
    "come up with a plan", "plan first", "multiple",
    "integration", "api", "webhook", "rate limit",
}

# Keywords that suggest a simple task
SIMPLE_KEYWORDS = {
    "typo", "copy", "text change", "rename", "bump", "update version",
    "config", "env", "readme", "comment", "log", "lint", "format",
}

# Patterns that indicate a query/research task — no code needed
NO_CODE_PATTERNS = [
    r"\bsort\b.*\bwhich\b",
    r"\blist\b.*\bfree\b",
    r"\bcompare\b",
    r"\bexplain\b",
    r"\bsummar(y|ize|ise)\b",
    r"\bwhat (is|are|does)\b",
    r"\bhow (do|does|to|can)\b",
    r"\bwhy (is|are|does|do)\b",
    r"\brecommend\b",
    r"\bfind\b.*\b(cheapest|best|top|free)\b",
    r"\bresearch\b",
    r"\banalyze\b.*\b(market|competitor|trend)\b",
    r"\bwhich\b.*\b(should|better|best)\b",
    r"\bpros and cons\b",
]

# Threshold: if description has this many complex keyword matches AND
# is long, classify as complex-large (needs subtask decomposition)
COMPLEX_LARGE_KEYWORD_THRESHOLD = 3
COMPLEX_LARGE_WORD_THRESHOLD = 60


def classify_task(title: str, description: str) -> tuple[TaskComplexity, ClassificationResult]:
    """Classify a task as simple, complex, complex-large, or simple-no-code."""
    text = f"{title} {description}".lower()
    word_count = len(text.split())

    # Check for query/research tasks first — no code needed
    for pattern in NO_CODE_PATTERNS:
        if re.search(pattern, text):
            result = ClassificationResult(
                classification="simple_no_code",
                reasoning=f"Query/research task: matched pattern '{pattern}'",
                estimated_files=0,
                risk=RiskLevel.LOW,
            )
            log.info(f"Classified as simple_no_code: matched '{pattern}'")
            return TaskComplexity.SIMPLE_NO_CODE, result

    # Count complex keyword matches
    complex_matches = []
    for kw in COMPLEX_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', text):
            complex_matches.append(kw)

    # Complex-large: many complex signals + long description
    if (len(complex_matches) >= COMPLEX_LARGE_KEYWORD_THRESHOLD
            and word_count >= COMPLEX_LARGE_WORD_THRESHOLD):
        result = ClassificationResult(
            classification="complex",
            reasoning=f"Large-scope task: {len(complex_matches)} complex keywords ({', '.join(complex_matches[:5])}), {word_count} words",
            estimated_files=10,
            risk=RiskLevel.HIGH,
        )
        log.info(f"Classified as complex_large: {len(complex_matches)} keywords, {word_count} words")
        return TaskComplexity.COMPLEX_LARGE, result

    # Standard complex: at least one complex keyword match
    if complex_matches:
        result = ClassificationResult(
            classification="complex",
            reasoning=f"Matched complex keyword: '{complex_matches[0]}'",
            estimated_files=5,
            risk=RiskLevel.MEDIUM,
        )
        log.info(f"Classified as complex: matched '{complex_matches[0]}'")
        return TaskComplexity.COMPLEX, result

    for kw in SIMPLE_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', text):
            result = ClassificationResult(
                classification="simple",
                reasoning=f"Matched simple keyword: '{kw}'",
                estimated_files=1,
                risk=RiskLevel.LOW,
            )
            log.info(f"Classified as simple: matched '{kw}'")
            return TaskComplexity.SIMPLE, result

    # Default: if description is short, probably simple
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
