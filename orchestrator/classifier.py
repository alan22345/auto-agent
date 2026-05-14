"""Task complexity classifier — thin async wrapper around the LLM-driven
classifier in :mod:`agent.classifier`.

The orchestrator pipeline needs a ``(TaskComplexity, ClassificationResult)``
tuple; the agent-layer classifier returns the Pydantic result alone. This
module bridges the two and is the single import point for callers like
``run.on_task_created``.

The keyword/word-count heuristic that used to live here was deleted —
matching titles like "research" or "compare" mis-routed real coding
tasks (e.g. marketing-copy landing pages) and small word counts
under-classified multi-file features. Bucketing is now driven entirely
by the LLM's answers to the three questions documented in
``agent.classifier``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent.classifier import classify_task as _classify_with_llm
from shared.models import TaskComplexity

if TYPE_CHECKING:
    from shared.types import ClassificationResult

log = logging.getLogger(__name__)

_LABEL_TO_COMPLEXITY: dict[str, TaskComplexity] = {
    "simple": TaskComplexity.SIMPLE,
    "complex": TaskComplexity.COMPLEX,
    "complex_large": TaskComplexity.COMPLEX_LARGE,
}


async def classify_task(
    title: str, description: str
) -> tuple[TaskComplexity, ClassificationResult]:
    """Classify ``title``/``description`` via the LLM-driven classifier.

    Returns the DB enum alongside the Pydantic result. Unknown labels
    fall back to ``COMPLEX`` so the pipeline always progresses.
    """
    result = await _classify_with_llm(title, description)
    complexity = _LABEL_TO_COMPLEXITY.get(result.classification, TaskComplexity.COMPLEX)
    log.info(
        "Classified task as %s: %s",
        complexity.value,
        result.reasoning,
    )
    return complexity, result
