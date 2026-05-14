"""Architect autocompact pin policy — ADR-015 §13 / Phase 6.

The architect's session compacts aggressively but must NEVER lose the
three load-bearing artefacts:

  - ``.auto-agent/design.md`` (the approved design doc)
  - ``.auto-agent/backlog.json`` (the current backlog)
  - ``.auto-agent/decision.json`` (the cycle's current decision)

Strategy: keep these artefacts OUT of the message buffer entirely. They
ride in the system prompt as a "pinned artefacts" section that the
architect agent factory re-attaches fresh on every resume. The session's
user/assistant turns can compact freely; the pinned blocks survive
because they're part of the system prompt — and autocompact only
rewrites the message buffer, never the system string.

Two helpers:

- :func:`build_pinned_block` reads each artefact off disk and returns
  a single markdown string with one section header per file. Missing
  files are silently skipped so the helper is safe to call before the
  artefacts have been written.
- :func:`apply_pinned_artefacts_to_system_prompt` appends the pinned
  block to a caller-supplied base prompt — the architect factory wraps
  every resume with this so the agent always sees the freshest copy.
"""

from __future__ import annotations

import os

from agent.lifecycle.workspace_paths import (
    BACKLOG_PATH,
    DECISION_PATH,
    DESIGN_PATH,
)

_SECTION_HEADER = "## .auto-agent/{name}"


def _read(path: str) -> str | None:
    """Return file contents, or ``None`` when the file doesn't exist."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def build_pinned_block(workspace_root: str) -> str:
    """Build the pinned-artefacts markdown block for ``workspace_root``.

    Each present artefact gets its own section header so the architect
    agent can locate it inside the system prompt. Missing artefacts are
    skipped — the block is best-effort and safe to call before any
    artefact exists. Returns an empty string when none of the three
    files are on disk.
    """

    sections: list[str] = []

    design = _read(os.path.join(workspace_root, DESIGN_PATH))
    if design is not None:
        sections.append(_SECTION_HEADER.format(name="design.md") + "\n\n" + design.rstrip() + "\n")

    backlog = _read(os.path.join(workspace_root, BACKLOG_PATH))
    if backlog is not None:
        sections.append(
            _SECTION_HEADER.format(name="backlog.json")
            + "\n\n```json\n"
            + backlog.rstrip()
            + "\n```\n"
        )

    decision = _read(os.path.join(workspace_root, DECISION_PATH))
    if decision is not None:
        sections.append(
            _SECTION_HEADER.format(name="decision.json")
            + "\n\n```json\n"
            + decision.rstrip()
            + "\n```\n"
        )

    if not sections:
        return ""

    intro = (
        "# Pinned artefacts (re-attached every resume — never compacted)\n\n"
        "These three files are the architect's load-bearing context. They\n"
        "live on disk under `.auto-agent/` and are the source of truth even\n"
        "if the message buffer is compacted away. Re-read them via\n"
        "`Read` whenever the running buffer drops detail.\n\n"
    )
    return intro + "\n".join(sections)


def apply_pinned_artefacts_to_system_prompt(
    base_prompt: str,
    workspace_root: str,
) -> str:
    """Append the pinned artefacts block to a base system prompt.

    Idempotent and side-effect-free; safe to call on every resume.
    Returns ``base_prompt`` unchanged when no artefacts exist.
    """

    block = build_pinned_block(workspace_root)
    if not block:
        return base_prompt
    return base_prompt.rstrip() + "\n\n" + block


__all__ = [
    "apply_pinned_artefacts_to_system_prompt",
    "build_pinned_block",
]
