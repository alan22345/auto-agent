"""Structural + no-defer backlog validator — ADR-015 §8 / §9 / Phase 6.

The trio architect's backlog submission is validated structurally before
the orchestrator dispatches any item:

  - ``title: str`` (non-empty)
  - ``description: str`` with ≥80 whitespace-split words
  - ``justification: str`` (non-empty)
  - ``affected_routes: list[str]`` (may be empty, but the field must exist)
  - ``affected_files_estimate: int`` (≥1)

After the structural check, each text field (title, description,
justification) is swept for the no-defer phrases listed in ADR-015 §8
layer 2. The ``# auto-agent: allow-stub`` opt-out applies to code lines
in a diff (§8 layer 3) — it does NOT exempt backlog text.

Returns a :class:`ValidationResult` carrying a list of
:class:`Rejection` rows so the architect's next turn can fix items one
by one without re-discovering which item failed which check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Rejection:
    """A single failed check for one backlog item.

    Attributes:
        item_index: 0-based index of the item in the submitted list.
        field: The dotted field name that failed
            (``title`` / ``description`` / ``justification`` /
            ``affected_routes`` / ``affected_files_estimate``).
        reason: Short human-readable description of the failure.
    """

    item_index: int
    field: str
    reason: str


@dataclass
class ValidationResult:
    """Outcome of validating a backlog submission.

    Attributes:
        ok: ``True`` only when every item passed every check.
        rejections: One row per failed check.
    """

    ok: bool
    rejections: list[Rejection] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Required structural fields per item.
# ---------------------------------------------------------------------------

_REQUIRED_TEXT_FIELDS = ("title", "description", "justification")
_REQUIRED_FIELDS = (
    "title",
    "description",
    "justification",
    "affected_routes",
    "affected_files_estimate",
)

_MIN_DESCRIPTION_WORDS = 80


# ---------------------------------------------------------------------------
# No-defer phrase patterns (ADR-015 §8 layer 2) — case-insensitive sweep
# over title + description + justification.
# ---------------------------------------------------------------------------


# Phase 9 (ADR-015 §8) extends the original set with the variants
# adversarial agents emit to dodge the obvious ``# Phase N`` form:
# the hyphen variant (``# Phase-N``), the lowercase variant
# (``# phase N`` / ``# phase-N``), and the colon-suffixed variant
# (``# Phase N:`` / ``# Phase-N:``). All variants are folded into a
# single combined regex (``# Phase\s*-?\s*\d+\s*:?``) — case-insensitive,
# matching the verify_primitives.grep_diff_for_stubs pattern set.
_NO_DEFER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "raise NotImplementedError",
        re.compile(r"\braise\s+NotImplementedError\b", re.IGNORECASE),
    ),
    (
        "# TODO(phase",
        re.compile(r"#\s*TODO\(\s*phase\b", re.IGNORECASE),
    ),
    (
        "# Phase N",
        re.compile(r"#\s*Phase\s*-?\s*\d+\s*:?", re.IGNORECASE),
    ),
    (
        "Phase 1",
        re.compile(r"\bPhase\s*-?\s*\d+\b", re.IGNORECASE),
    ),
    (
        "v2 ships",
        re.compile(r"\bv2\s+ships\b", re.IGNORECASE),
    ),
    (
        "will be implemented later",
        re.compile(r"\bwill\s+be\s+implemented\s+later\b", re.IGNORECASE),
    ),
    (
        "for now this is a stub",
        re.compile(r"\bfor\s+now\s+this\s+is\s+a\s+stub\b", re.IGNORECASE),
    ),
)


def _no_defer_violation(text: str) -> str | None:
    """Return the matched pattern label, or ``None`` if the text is clean."""
    if not text:
        return None
    for label, pattern in _NO_DEFER_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _word_count(text: str) -> int:
    """Whitespace-split word count — matches the spec language exactly."""
    return len(text.split()) if text else 0


def validate_backlog(items: object) -> ValidationResult:
    """Validate a backlog submission.

    Args:
        items: The ``items`` list from ``backlog.json``.

    Returns:
        :class:`ValidationResult` — ``ok=True`` only when every item
        passes every check; otherwise ``rejections`` lists one row per
        failure (one item can have multiple failures — each gets its own
        row so the architect can fix them all in the next turn).
    """

    rejections: list[Rejection] = []

    if not isinstance(items, list):
        rejections.append(
            Rejection(
                item_index=-1,
                field="items",
                reason="backlog must be a JSON list",
            )
        )
        return ValidationResult(ok=False, rejections=rejections)

    if not items:
        rejections.append(
            Rejection(
                item_index=-1,
                field="items",
                reason="backlog must contain at least one item",
            )
        )
        return ValidationResult(ok=False, rejections=rejections)

    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            rejections.append(
                Rejection(
                    item_index=index,
                    field="<item>",
                    reason="each backlog item must be a JSON object",
                )
            )
            continue

        # Required fields presence + type.
        for required in _REQUIRED_FIELDS:
            if required not in raw:
                rejections.append(
                    Rejection(
                        item_index=index,
                        field=required,
                        reason=f"missing required field '{required}'",
                    )
                )

        # Text fields non-empty.
        for field_name in _REQUIRED_TEXT_FIELDS:
            value = raw.get(field_name)
            if field_name in raw and (not isinstance(value, str) or not value.strip()):
                rejections.append(
                    Rejection(
                        item_index=index,
                        field=field_name,
                        reason=f"'{field_name}' must be a non-empty string",
                    )
                )

        # Description word-count floor.
        desc = raw.get("description")
        if isinstance(desc, str) and _word_count(desc) < _MIN_DESCRIPTION_WORDS:
            rejections.append(
                Rejection(
                    item_index=index,
                    field="description",
                    reason=(
                        f"description must be ≥{_MIN_DESCRIPTION_WORDS} whitespace-split "
                        f"words (got {_word_count(desc)})"
                    ),
                )
            )

        # affected_routes must be a list of strings (possibly empty).
        routes = raw.get("affected_routes")
        if "affected_routes" in raw and (
            not isinstance(routes, list) or not all(isinstance(r, str) for r in routes)
        ):
            rejections.append(
                Rejection(
                    item_index=index,
                    field="affected_routes",
                    reason="'affected_routes' must be a list of strings",
                )
            )

        # affected_files_estimate must be a positive integer.
        est = raw.get("affected_files_estimate")
        if "affected_files_estimate" in raw:
            valid_int = isinstance(est, int) and not isinstance(est, bool)
            if not valid_int or est < 1:
                rejections.append(
                    Rejection(
                        item_index=index,
                        field="affected_files_estimate",
                        reason="'affected_files_estimate' must be an integer ≥1",
                    )
                )

        # No-defer sweep across the three text fields.
        for field_name in _REQUIRED_TEXT_FIELDS:
            value = raw.get(field_name)
            if isinstance(value, str):
                label = _no_defer_violation(value)
                if label is not None:
                    rejections.append(
                        Rejection(
                            item_index=index,
                            field=field_name,
                            reason=(f"forbidden no-defer phrase in {field_name}: {label!r}"),
                        )
                    )

    return ValidationResult(ok=not rejections, rejections=rejections)


__all__ = ["Rejection", "ValidationResult", "validate_backlog"]
