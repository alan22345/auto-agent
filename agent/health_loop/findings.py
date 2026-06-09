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
