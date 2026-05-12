"""Picker rendering + stateless pick-resolution.

The picker is stateless: when a user replies with a number or 'new', we
parse it against the user's current active-task list. No transient store.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.messenger_router.types import FocusKind

_PICK_RE = re.compile(r"^\s*#?(\d+)\s*$")
_NEW_RE = re.compile(r"^\s*new\s*$", re.IGNORECASE)


def parse_pick(
    text: str,
    *,
    active_task_ids: list[int],
) -> tuple[FocusKind, int | None] | None:
    """Try to interpret ``text`` as a picker reply.

    Returns ``('task', id)`` for a numeric pick whose id is in
    ``active_task_ids``; ``('draft', None)`` for a 'new' reply; or
    ``None`` if the text doesn't look like a pick.
    """
    if _NEW_RE.match(text):
        return ("draft", None)
    m = _PICK_RE.match(text)
    if m:
        task_id = int(m.group(1))
        if task_id in active_task_ids:
            return ("task", task_id)
    return None


def render_picker(active_tasks: list[dict[str, Any]]) -> str:
    """Render the picker message body. Active tasks come pre-sorted."""
    if not active_tasks:
        return "You don't have any active tasks. Reply `new` to start a fresh request."
    lines = ["Which task do you want to pick up?"]
    for i, t in enumerate(active_tasks, start=1):
        title = (t.get("title") or "")[:80]
        status = t.get("status", "")
        lines.append(f"{i}. #{t['id']}  {title}  ({status})")
    lines.append("Reply with the task number (e.g. 42) or `new` to start fresh.")
    return "\n".join(lines)
