"""Rank code-graph health findings into an ordered, deduplicated work list.

Pure functions over :mod:`shared.types` — no DB, no I/O. Given a
``RepoGraphBlob`` they produce a list of :class:`HealthFinding` ordered
worst-first, each carrying a stable ``finding_hash`` so the loop never
double-files or re-picks a suppressed finding.
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel

from shared.types import RepoGraphBlob

Category = Literal["poor_file", "dead_code", "clone", "hotspot", "cycle"]

# Mirrors the composite-health sub-score weighting in
# agent/graph_analyzer/health.py. 'coupling' (0.15) has no per-item finding
# list of its own, so 'hotspot' takes that slot.
CATEGORY_WEIGHTS: dict[Category, float] = {
    "poor_file": 0.30,
    "dead_code": 0.25,
    "clone": 0.20,
    "hotspot": 0.15,
    "cycle": 0.10,
}


def finding_hash(category: Category, parts: list[str]) -> str:
    """Stable 16-char identity for a finding.

    ``parts`` are the identity-bearing strings for the category (e.g. the
    dead-code target, the sorted cycle members, the clone family). Sorted
    before hashing so member ordering can't change the hash.
    """
    canonical = category + "|" + "|".join(sorted(parts))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


class HealthFinding(BaseModel, frozen=True):
    """One actionable health finding, normalized across categories.

    ``finding_hash`` is stable across re-analyses (see :func:`finding_hash`)
    so it doubles as the dedup / suppression key. ``severity`` is the
    in-category magnitude (higher = worse), used as the secondary sort key.
    """

    finding_hash: str
    category: Category
    title: str
    files: list[str]
    severity: float


def extract_findings(blob: RepoGraphBlob) -> list[HealthFinding]:
    """Flatten a blob into normalized :class:`HealthFinding` records.

    One finding per dead-code item, cycle, clone group, hotspot, and
    'poor'-band file. 'moderate'/'good' files are not findings.
    """
    out: list[HealthFinding] = []

    for d in blob.dead_code:
        out.append(HealthFinding(
            finding_hash=finding_hash("dead_code", [d.target]),
            category="dead_code",
            title=f"{d.kind}: {d.target} — {d.reason}",
            files=[d.file] if d.file else [],
            severity=1.0,
        ))

    for c in blob.cycles:
        out.append(HealthFinding(
            finding_hash=finding_hash("cycle", list(c.members)),
            category="cycle",
            title=f"import cycle [{c.kind}]: {' → '.join(c.members)}",
            files=list(dict.fromkeys(m.split('::')[0] for m in c.members)),
            severity=float(len(c.members)),
        ))

    for g in blob.clones:
        files = list(dict.fromkeys(inst.file for inst in g.instances))
        out.append(HealthFinding(
            finding_hash=finding_hash("clone", [g.family_id] if g.family_id else
                                      [f"{inst.file}:{inst.line_start}-{inst.line_end}" for inst in g.instances]),
            category="clone",
            title=f"clone group {g.id} — {g.token_len} tokens, {len(g.instances)} instances",
            files=files,
            severity=float(g.token_len),
        ))

    for h in blob.hotspots:
        out.append(HealthFinding(
            finding_hash=finding_hash("hotspot", [h.file]),
            category="hotspot",
            title=f"hotspot {h.file} — score {h.score:.1f} ({h.trend})",
            files=[h.file],
            severity=float(h.score),
        ))

    for fh in blob.file_health:
        if fh.band != "poor":
            continue
        out.append(HealthFinding(
            finding_hash=finding_hash("poor_file", [fh.file]),
            category="poor_file",
            title=f"poor maintainability {fh.file} — index {fh.maintainability_index:.1f}",
            files=[fh.file],
            severity=100.0 - fh.maintainability_index,
        ))

    return out
