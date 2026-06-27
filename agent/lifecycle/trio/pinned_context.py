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
    format_design_header,
    slice_backlog_path,
    slice_decision_path,
    slice_design_path,
)

_SECTION_HEADER = "## {name}"


def _read(path: str) -> str | None:
    """Return file contents, or ``None`` when the file doesn't exist."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def _paths_for(slice_name: str | None) -> tuple[str, str, str]:
    """Return the (design, backlog, decision) relative paths.

    When ``slice_name`` is set, the slice-scoped paths under
    ``.auto-agent/slices/<name>/`` are returned; otherwise the root
    workspace paths. Sub-architects pin their own slice artefacts so the
    parent architect's design / backlog / decision don't bleed into the
    sub-architect's system prompt (and vice versa).
    """

    if slice_name:
        return (
            slice_design_path(slice_name),
            slice_backlog_path(slice_name),
            slice_decision_path(slice_name),
        )
    return DESIGN_PATH, BACKLOG_PATH, DECISION_PATH


def _design_belongs_to_task(design: str, task_id: int | None) -> bool:
    """True if the design.md header matches ``task_id`` (or no task_id
    constraint is given).

    Stale design.md guard (task 29, 2026-05-27): a prior task's
    integration PR can leave ``.auto-agent/design.md`` committed on the
    base branch. The next task that clones that branch will pick it up
    and — if pinned blindly — convince the architect that "the design
    is already done" using the wrong task's content. The fix: pinned
    context skips a design.md whose ``<!-- auto-agent: task_id=N -->``
    header doesn't match the current task. The architect's first turn
    then sees no pinned design, runs the design pass fresh, writes the
    correct artefact, and the gate proceeds normally.

    Backwards-compat: when ``task_id`` is ``None`` (slice context, old
    callsites without the kwarg) we accept any header — the legacy
    behaviour. When the design.md has no header at all we also accept
    (very old artefacts predate the header convention).
    """
    if task_id is None:
        return True
    expected = format_design_header(task_id)
    for line in design.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<!-- auto-agent: task_id="):
            return stripped == expected
        # First non-empty line was not a header — treat as legacy / no
        # header. Accept; we'd rather over-include than over-exclude.
        return True
    return True


def build_pinned_block(
    workspace_root: str,
    *,
    slice_name: str | None = None,
    task_id: int | None = None,
) -> str:
    """Build the pinned-artefacts markdown block for ``workspace_root``.

    Each present artefact gets its own section header so the architect
    agent can locate it inside the system prompt. Missing artefacts are
    skipped — the block is best-effort and safe to call before any
    artefact exists. Returns an empty string when none of the three
    files are on disk.

    When ``slice_name`` is supplied the slice-scoped paths under
    ``.auto-agent/slices/<name>/`` are pinned instead of the root
    workspace files — this is the namespace boundary for sub-architects
    (ADR-015 §9 / §13).

    When ``task_id`` is supplied the design.md's ``<!-- auto-agent:
    task_id=N -->`` header is checked; a mismatched header (i.e.
    leftover from a prior task whose integration PR committed
    ``.auto-agent/design.md`` to the base branch) causes the file to
    be treated as missing — see :func:`_design_belongs_to_task`.
    """

    design_rel, backlog_rel, decision_rel = _paths_for(slice_name)
    sections: list[str] = []

    design = _read(os.path.join(workspace_root, design_rel))
    if design is not None and not _design_belongs_to_task(design, task_id):
        import structlog

        log = structlog.get_logger()
        log.warning(
            "trio.pinned_context.stale_design_md_skipped",
            workspace_root=workspace_root,
            task_id=task_id,
            head_line=design.splitlines()[0] if design.splitlines() else "",
        )
        design = None
    if design is not None:
        sections.append(_SECTION_HEADER.format(name=design_rel) + "\n\n" + design.rstrip() + "\n")

    backlog = _read(os.path.join(workspace_root, backlog_rel))
    if backlog is not None:
        sections.append(
            _SECTION_HEADER.format(name=backlog_rel)
            + "\n\n```json\n"
            + backlog.rstrip()
            + "\n```\n"
        )

    decision = _read(os.path.join(workspace_root, decision_rel))
    if decision is not None:
        sections.append(
            _SECTION_HEADER.format(name=decision_rel)
            + "\n\n```json\n"
            + decision.rstrip()
            + "\n```\n"
        )

    if not sections:
        return ""

    scope_label = f"sub-architect slice `{slice_name}`" if slice_name else "architect"
    location_label = f"`.auto-agent/slices/{slice_name}/`" if slice_name else "`.auto-agent/`"
    intro = (
        "# Pinned artefacts (re-attached every resume — never compacted)\n\n"
        f"These three files are the {scope_label}'s load-bearing context.\n"
        f"They live on disk under {location_label} and are the source of\n"
        "truth even if the message buffer is compacted away. Re-read them\n"
        "via `Read` whenever the running buffer drops detail.\n\n"
    )
    return intro + "\n".join(sections)


def apply_pinned_artefacts_to_system_prompt(
    base_prompt: str,
    workspace_root: str,
    *,
    slice_name: str | None = None,
    task_id: int | None = None,
) -> str:
    """Append the pinned artefacts block to a base system prompt.

    Idempotent and side-effect-free; safe to call on every resume.
    Returns ``base_prompt`` unchanged when no artefacts exist.

    ``slice_name`` toggles between root and slice-scoped pin sources;
    ``task_id`` enables stale-design-md filtering — see
    :func:`build_pinned_block`.
    """

    block = build_pinned_block(workspace_root, slice_name=slice_name, task_id=task_id)
    if not block:
        return base_prompt
    return base_prompt.rstrip() + "\n\n" + block


__all__ = [
    "apply_pinned_artefacts_to_system_prompt",
]
