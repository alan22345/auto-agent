"""Architect externalized journal — ADR-015 §13 / Phase 6.

The architect's session compacts aggressively across many cycles. To
keep the decision history durable beyond the working buffer, every
decision (design submission, backlog submission, per-cycle checkpoint)
appends a row to ``.auto-agent/architect_log.md`` plus a per-decision
detail file ``.auto-agent/decisions/<seq>.json``.

The log is human-readable markdown; the detail files are the source of
truth that the architect can re-read on demand when it needs context
about a prior decision (e.g. "why did I slice item 3 the way I did").
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from agent.lifecycle.workspace_paths import (
    ARCHITECT_LOG_PATH,
    AUTO_AGENT_DIR,
    decision_history_path,
)


def _decisions_dir(workspace_root: str) -> str:
    return os.path.join(workspace_root, AUTO_AGENT_DIR, "decisions")


def next_decision_seq(workspace_root: str) -> int:
    """Return the next sequence number (1-indexed, monotonic).

    Scans ``.auto-agent/decisions/`` for ``<n>.json`` files and returns
    ``max(n) + 1``; falls back to ``1`` when the directory is missing or
    empty.
    """

    directory = _decisions_dir(workspace_root)
    if not os.path.isdir(directory):
        return 1

    highest = 0
    for entry in os.listdir(directory):
        if not entry.endswith(".json"):
            continue
        stem = entry[: -len(".json")]
        if not stem.isdigit():
            continue
        highest = max(highest, int(stem))
    return highest + 1


def append_journal_entry(
    workspace_root: str,
    *,
    decision: dict[str, Any],
    rationale: str,
) -> int:
    """Append one decision to the journal and write its detail file.

    Writes:
      - ``.auto-agent/architect_log.md`` — one section per decision with
        the timestamp, action, rationale preview, and a pointer to the
        detail file.
      - ``.auto-agent/decisions/<seq>.json`` — ``{decision, rationale,
        timestamp}`` for the full payload.

    Args:
        workspace_root: Absolute path to the workspace root.
        decision: The decision payload as written via the
            ``submit-architect-decision`` skill (already validated).
        rationale: The architect's prose accompanying the decision —
            the load-bearing context for "why did I do that".

    Returns:
        The sequence number assigned to this entry (1-indexed).
    """

    seq = next_decision_seq(workspace_root)
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    action = str(decision.get("action", "<unknown>"))

    # Detail file — full payload + rationale + timestamp.
    detail_dir = _decisions_dir(workspace_root)
    os.makedirs(detail_dir, exist_ok=True)
    detail_path = os.path.join(workspace_root, decision_history_path(seq))
    with open(detail_path, "w") as fh:
        json.dump(
            {
                "decision": decision,
                "rationale": rationale,
                "timestamp": timestamp,
            },
            fh,
            indent=2,
        )

    # Log entry — append (never rewrite).
    log_dir = os.path.join(workspace_root, AUTO_AGENT_DIR)
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(workspace_root, ARCHITECT_LOG_PATH)

    # Short prose preview — single paragraph, indented under the header.
    preview = (rationale or "").strip()
    if not preview:
        preview = "(no rationale provided)"

    detail_rel = decision_history_path(seq)
    entry = (
        f"## {timestamp} — {action}\n\n**Reason:** {preview}\n\n**Detail:** see {detail_rel}\n\n"
    )

    if os.path.isfile(log_path):
        with open(log_path, "a") as fh:
            fh.write(entry)
    else:
        with open(log_path, "w") as fh:
            fh.write(entry)

    return seq


__all__ = ["append_journal_entry", "next_decision_seq"]
