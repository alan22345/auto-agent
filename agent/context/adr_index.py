"""Pure ADR parsing + active-index rendering for docs/decisions/.

No I/O beyond reading the ADR files it is handed. Used by the system-prompt
builder (live, status-aware injection), record_decision (INDEX regeneration),
and the review consistency gate.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

_NUM_RE = re.compile(r"^(\d{3})-")
_H1_RE = re.compile(r"^#\s+(?:\[ADR-\d+\]\s+)?(.*\S)\s*$")
_SUMMARY_RE = re.compile(r"^>\s*\*\*Summary:\*\*\s*(.*\S)\s*$")
_SUPERSEDED_BY_RE = re.compile(r"superseded\s+by\s+\[?ADR-(\d+)\]?", re.IGNORECASE)


@dataclass(frozen=True)
class AdrMeta:
    number: int
    path: str
    title: str
    status: str                       # full first non-empty line under ## Status
    summary: str | None = None
    superseded_by: int | None = None


def status_kind(status: str) -> str:
    """Classify a status line by its leading keyword."""
    s = status.strip().lower()
    if s.startswith("superseded"):
        return "superseded"
    if s.startswith("deprecated"):
        return "deprecated"
    if s.startswith("accepted"):
        return "accepted"
    if s.startswith("proposed"):
        return "proposed"
    return "unknown"


def parse_adr(path: str) -> AdrMeta:
    name = os.path.basename(path)
    m = _NUM_RE.match(name)
    number = int(m.group(1)) if m else -1

    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    title = name
    summary: str | None = None
    status = ""
    in_status = False
    for i, line in enumerate(lines):
        h1 = _H1_RE.match(line)
        if h1 and title == name:
            title = h1.group(1)
            continue
        sm = _SUMMARY_RE.match(line)
        if sm and summary is None:
            summary = sm.group(1)
            continue
        if line.strip().lower() == "## status":
            in_status = True
            continue
        if in_status:
            if line.strip().startswith("## "):
                in_status = False
            elif line.strip():
                status = line.strip()
                in_status = False

    superseded_by = None
    sb = _SUPERSEDED_BY_RE.search(status)
    if sb:
        superseded_by = int(sb.group(1))

    return AdrMeta(
        number=number, path=path, title=title,
        status=status, summary=summary, superseded_by=superseded_by,
    )
