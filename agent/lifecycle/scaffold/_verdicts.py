"""Shared reader for per-domain ADR verdict files — ADR-018 §6/§7.

Both the Phase C-gate (``domain_adr_approval``) and the Phase D dispatcher
(``dispatch_children``) need to read every ``<slug>.json`` under
``.auto-agent/domain_adr_approvals/``. The reader lives here so the two
phases share one implementation.
"""

from __future__ import annotations

import json
import os
from typing import Any

from agent.lifecycle.workspace_paths import DOMAIN_ADR_APPROVALS_DIR


def read_all_verdicts(workspace: str) -> dict[str, dict[str, Any]]:
    """Read every ``<slug>.json`` under ``domain_adr_approvals/``.

    Returns a ``{slug: payload}`` mapping. Files that are missing, not
    JSON, or not a JSON object are skipped.
    """

    dir_abs = os.path.join(workspace, DOMAIN_ADR_APPROVALS_DIR)
    if not os.path.isdir(dir_abs):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in os.listdir(dir_abs):
        if not entry.endswith(".json"):
            continue
        slug = entry[: -len(".json")]
        try:
            with open(os.path.join(dir_abs, entry)) as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            out[slug] = payload
    return out


__all__ = ["read_all_verdicts"]
