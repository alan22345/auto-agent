"""Soft-retire an ADR in place: flip its Status, keep the file."""
from __future__ import annotations

import os
import re

_NUM_RE = re.compile(r"^(\d{3})-")
_STATUS_HDR = "## Status"


def _find_adr(adr_dir: str, number: int) -> str | None:
    if not os.path.isdir(adr_dir):
        return None
    for name in os.listdir(adr_dir):
        m = _NUM_RE.match(name)
        if m and int(m.group(1)) == number and name.endswith(".md"):
            return os.path.join(adr_dir, name)
    return None


def retire_adr(adr_dir: str, number: int, *, by_number: int) -> bool:
    """Set ADR ``number``'s status to 'Superseded by [ADR-by_number]'.

    Replaces the first non-empty line under '## Status'. Returns False if the
    ADR file is not found. The file is never deleted.
    """
    path = _find_adr(adr_dir, number)
    if path is None:
        return False

    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        out.append(lines[i])
        if lines[i].strip() == _STATUS_HDR and not replaced:
            i += 1
            # copy blank lines after the header
            while i < len(lines) and not lines[i].strip():
                out.append(lines[i])
                i += 1
            # replace the first status value line
            if i < len(lines):
                out.append(f"Superseded by [ADR-{by_number:03d}]")
                i += 1
            replaced = True
            continue
        i += 1

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return replaced
