"""LLM-driven task classifier.

Buckets each task by asking the model three questions, in order:

1. Is this purely a UI visual change (copy/colour/layout/styling, no logic)?
   yes -> ``simple``.
2. Otherwise, would this naturally need to be split into several stages /
   independent chunks to complete?
   yes -> ``complex_large``.
3. Otherwise -> ``complex``.

The classifier also returns ``needs_grill`` (ADR-015 §1) — orthogonal to the
bucket. ``True`` ⇒ the task is ambiguous and the flow must run grill-me
before any planning / design / one-shot; ``False`` ⇒ the ask is unambiguous
enough to skip the grill phase regardless of bucket.

There is no keyword matching or word-count heuristic — those misclassified
real work (e.g. marketing-copy pages that mention "research" routed through
the no-code path). When the LLM call fails outright, we default to
``complex`` as the safe middle bucket and ``needs_grill=True`` as the safe
grill path (better to clarify a known task than to skip clarification on an
ambiguous one).
"""

from __future__ import annotations

import logging

from agent.llm import get_provider
from agent.llm.structured import complete_json
from agent.llm.types import Message
from shared.types import ClassificationResult, RiskLevel

log = logging.getLogger(__name__)

_VALID = {"simple", "complex", "complex_large"}

_SYSTEM = (
    "You classify software tasks by scope. "
    "Reply with ONE JSON object and nothing else."
)

_PROMPT = """Classify this task by answering, in order:

1. ui_only — Is this purely a UI visual change (copy edit, colour, font,
   spacing, swap an image, restyle an existing component) with NO new
   logic, no new data, no backend changes?
2. multi_stage — Would this naturally break into several independent
   stages to complete (multiple sequential phases, several large
   sub-features that depend on each other, or a body of work too large
   to land in one focused change)?
3. needs_grill — Has the user described the task with enough specificity
   that no clarification is needed? If the ask is unambiguous (clear
   scope, defined behaviour, all the inputs the implementer would need
   are present), set needs_grill=false. If anything is ambiguous (unclear
   scope, undefined behaviour, missing details, vague intent), set
   needs_grill=true. When in doubt, prefer true — the cost of one extra
   clarification round is far lower than the cost of building the wrong
   thing.

Rules:
- ui_only=true                        -> classification = "simple"
- ui_only=false AND multi_stage=true  -> classification = "complex_large"
- otherwise                           -> classification = "complex"

Title: {title}
Description: {description}

Output ONLY this JSON object:
{{"ui_only": <bool>, "multi_stage": <bool>, "needs_grill": <bool>, "classification": "simple|complex|complex_large", "reasoning": "<one sentence>"}}
"""

_RISK = {
    "simple": RiskLevel.LOW,
    "complex": RiskLevel.MEDIUM,
    "complex_large": RiskLevel.HIGH,
}
_ESTIMATED_FILES = {"simple": 1, "complex": 4, "complex_large": 10}


async def classify_task(title: str, description: str) -> ClassificationResult:
    """Return a ClassificationResult driven by the LLM's answers to the
    three bucketing questions. Defaults to ``complex`` on LLM failure."""
    try:
        provider = get_provider()
        data = await complete_json(
            provider,
            messages=[
                Message(
                    role="user",
                    content=_PROMPT.format(
                        title=title or "(no title)",
                        description=(description or "")[:2000],
                    ),
                )
            ],
            system=_SYSTEM,
            max_tokens=300,
            retries=2,
        )
    except Exception as exc:
        log.warning("LLM classification failed, defaulting to complex: %s", exc)
        return ClassificationResult(
            classification="complex",
            reasoning="LLM classifier unavailable; defaulted to complex",
            estimated_files=_ESTIMATED_FILES["complex"],
            risk=_RISK["complex"],
            # Safe default on classifier failure: grill before planning.
            needs_grill=True,
        )

    label = str(data.get("classification", "")).strip().lower()
    if label not in _VALID:
        # Re-derive from the two binary answers if the model emitted a
        # bogus label but gave us the questions.
        ui_only = bool(data.get("ui_only", False))
        multi_stage = bool(data.get("multi_stage", False))
        if ui_only:
            label = "simple"
        elif multi_stage:
            label = "complex_large"
        else:
            label = "complex"

    # ADR-015 §1: needs_grill rides in the same JSON object as classification.
    # Default to True when the field is missing — safer to clarify an
    # already-clear task than to skip clarification on an ambiguous one.
    raw_needs_grill = data.get("needs_grill", True)
    needs_grill = bool(raw_needs_grill) if raw_needs_grill is not None else True

    reasoning = str(data.get("reasoning", "")).strip() or f"LLM-classified as {label}"
    return ClassificationResult(
        classification=label,
        reasoning=reasoning,
        estimated_files=_ESTIMATED_FILES[label],
        risk=_RISK[label],
        needs_grill=needs_grill,
    )
