"""Clarification-marker extraction shared across phases.

The grill-only marker (``GRILL_DONE``) lives next to the planner since only
the planner cares about it.
"""

from __future__ import annotations

from agent.prompts import CLARIFICATION_MARKER


def _extract_clarification(output: str) -> str | None:
    """Check if agent output contains a clarification request."""
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(CLARIFICATION_MARKER):
            first_line = line.strip()[len(CLARIFICATION_MARKER) :].strip()
            remaining = [line.strip() for line in lines[i + 1 :] if line.strip()]
            parts = [first_line] + remaining
            return "\n".join(parts)
    return None
