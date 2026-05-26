"""Promote approved ADRs from ``.auto-agent/adrs/`` → ``docs/decisions/``.

The scaffold gates keep reading from ``.auto-agent/adrs/`` (their working
set); on approval we *copy* the source file into ``docs/decisions/`` so
the project's canonical ADR directory holds the approved decision.
"""

from __future__ import annotations

import os
import shutil

import structlog

from agent.lifecycle.workspace_paths import docs_decision_path

log = structlog.get_logger()


def promote_adr_to_docs(workspace: str, src_rel: str) -> str | None:
    """Copy ``workspace/src_rel`` into ``workspace/docs/decisions/``.

    Filename is preserved (``000-system.md``, ``001-auth.md``,
    ``001-auth.grill.md``, …). Overwrites the destination if it already
    exists. Returns the destination absolute path on success, or
    ``None`` when the source file is missing.
    """

    src_abs = os.path.join(workspace, src_rel)
    if not os.path.isfile(src_abs):
        log.warning("scaffold.adr_promotion.source_missing", src=src_abs)
        return None

    filename = os.path.basename(src_rel)
    dst_abs = os.path.join(workspace, docs_decision_path(filename))
    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
    shutil.copyfile(src_abs, dst_abs)
    log.info("scaffold.adr_promotion.copied", src=src_abs, dst=dst_abs)
    return dst_abs


__all__ = ["promote_adr_to_docs"]
