"""Dedup-trap scorer (ADR-025): did the agent reuse the existing helper?

Each trap task asks for behaviour an existing helper already provides
(``trap_helper`` in ``trap_module``). The honest outcomes:

* reused the helper, wrote no duplicate  -> 1.0  (pass)
* reused it but also wrote a near-dup    -> 0.4  (fail)
* ignored it and wrote a duplicate       -> 0.0  (fail)
* neither (inlined the logic by hand)    -> 0.2  (fail)

"Reused" = the helper's name appears in added diff lines outside its
defining module. "Duplicated" = an added line defines a new function
matching ``trap_dup_pattern`` outside the defining module.
"""

from __future__ import annotations

import json
import re


def get_assert(output: str, context: dict) -> dict:
    test_vars = context.get("vars", {})
    helper = test_vars.get("trap_helper", "")
    trap_module = test_vars.get("trap_module", "")
    dup_pattern = test_vars.get("trap_dup_pattern", "")
    if not helper or not dup_pattern:
        return {"pass": False, "score": 0.0, "reason": "trap vars missing from test config"}

    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return {"pass": False, "score": 0.0, "reason": "provider output is not JSON"}

    added_lines = _added_diff_lines(payload.get("diff", ""))
    changed_files = payload.get("files", {}) or {}

    reused = _helper_reused(helper, trap_module, added_lines, changed_files)
    duplicated = _duplicate_definition(dup_pattern, added_lines)

    if reused and not duplicated:
        return {"pass": True, "score": 1.0, "reason": f"reused {helper}, no duplicate written"}
    if reused and duplicated:
        return {
            "pass": False,
            "score": 0.4,
            "reason": f"reused {helper} but ALSO wrote a near-duplicate",
        }
    if duplicated:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"wrote a duplicate instead of reusing {helper}",
        }
    return {
        "pass": False,
        "score": 0.2,
        "reason": f"neither reused {helper} nor defined a duplicate (logic likely inlined)",
    }


def _added_diff_lines(diff: str) -> list[str]:
    return [
        line[1:]
        for line in (diff or "").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def _helper_reused(
    helper: str,
    trap_module: str,
    added_lines: list[str],
    changed_files: dict[str, str],
) -> bool:
    """The helper's name shows up in new code outside its own module."""
    if any(helper in line for line in added_lines):
        return True
    return any(helper in content for path, content in changed_files.items() if path != trap_module)


def _duplicate_definition(dup_pattern: str, added_lines: list[str]) -> bool:
    """A new function matching the duplicate pattern appears in added code.

    Only added diff lines count — full changed-file contents include
    pre-existing code (the helper's own definition) and would
    false-positive.
    """
    pattern = re.compile(dup_pattern)
    return any(pattern.search(line) for line in added_lines)
