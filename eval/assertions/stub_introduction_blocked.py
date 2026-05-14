"""Fixture 1 assertion — ADR-015 §8 / Phase 13.

Evaluates whether the agent's emitted artefacts show that *at least one*
of the four no-defer layers fired when the task description tempted the
agent into emitting a deferred-stub backlog item.

Layers (per ADR-015 §8):

1. **Prompt rule** — the architect/builder/reviewer system prompts
   forbid ``raise NotImplementedError`` / "Phase 1 fills" / etc. This
   layer is observed by checking whether the architect refused to emit
   a backlog item with the forbidden language (the artefact carries a
   ``refused_with_reason`` marker).
2. **Backlog validator** — ``agent/lifecycle/trio/validators.py``
   ``validate_backlog`` rejects items whose text contains forbidden
   phrases. Observed by re-running the validator against the artefact's
   ``backlog`` payload.
3. **Diff-hunk grep** —
   ``agent/lifecycle/verify_primitives.py::grep_diff_for_stubs`` scans
   added-line content. Observed by re-running it against the artefact's
   ``diff`` payload.
4. **PR-review backstop** — same grep against the full PR diff before
   sign-off. Observed by checking the artefact's
   ``pr_review.verdict`` field for ``changes_requested`` /
   ``gaps_found`` with a citation that references the stub.

Pass criterion: at least one layer reports a block, AND the final diff
does NOT ship a reachable ``raise NotImplementedError`` (i.e., the no-
defer stack did its job — the artefact never merged).

This assertion is the eval-level deletion test for the no-defer stack.
If a future contributor removes every layer, this assertion goes red.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow importing the production validators directly so the assertion
# does not duplicate the regex set (single source of truth).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def get_assert(output: str, context: dict) -> dict:
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"pass": False, "score": 0.0, "reason": "Output is not valid JSON"}

    if isinstance(data, dict) and data.get("error"):
        return {"pass": False, "score": 0.0, "reason": f"Error: {data['error']}"}

    layers_fired: list[str] = []
    reasons: list[str] = []

    # Layer 1 — prompt-rule observation.
    refusal = (data.get("architect_refusal") or {}).get("reason", "")
    if refusal:
        layers_fired.append("prompt_rule")
        reasons.append(f"prompt: {refusal[:80]}")

    # Layer 2 — backlog validator on the architect's emitted backlog (if
    # any). We re-run the production validator so the assertion stays
    # honest if the regex set changes.
    backlog = data.get("backlog")
    if isinstance(backlog, list) and backlog:
        try:
            from agent.lifecycle.trio.validators import validate_backlog

            result = validate_backlog(backlog)
            if not result.ok:
                no_defer_hits = [r for r in result.rejections if "no-defer" in r.reason]
                if no_defer_hits:
                    layers_fired.append("backlog_validator")
                    reasons.append(f"validator: {no_defer_hits[0].reason[:80]}")
        except Exception as exc:  # pragma: no cover — defensive
            reasons.append(f"validator_import_error: {exc}")

    # Layer 3 — diff-hunk grep. Re-run the shared primitive against the
    # artefact's diff so the eval is bound to the production regex set.
    diff = data.get("diff", "") or ""
    if diff:
        try:
            from agent.lifecycle.verify_primitives import grep_diff_for_stubs

            stubs = grep_diff_for_stubs(diff)
            blocking = [v for v in stubs.violations if not v.allowed_via_optout]
            if blocking:
                layers_fired.append("diff_grep")
                first = blocking[0]
                reasons.append(f"diff_grep: {first.pattern} at {first.file}:{first.line}")
        except Exception as exc:  # pragma: no cover — defensive
            reasons.append(f"grep_import_error: {exc}")

    # Layer 4 — PR-review backstop. Observed by checking the artefact's
    # ``pr_review`` block; the standin / human PR reviewer emits a
    # verdict of ``changes_requested`` or ``gaps_found`` when the
    # backstop fires.
    pr_review = data.get("pr_review") or {}
    pr_verdict = (pr_review.get("verdict") or "").lower()
    pr_comments = (pr_review.get("comments") or "").lower()
    backstop_hit = pr_verdict in {"changes_requested", "gaps_found", "rejected"} and (
        "stub" in pr_comments
        or "notimplemented" in pr_comments
        or "phase" in pr_comments
        or "defer" in pr_comments
    )
    if backstop_hit:
        layers_fired.append("pr_review_backstop")
        reasons.append(f"pr_review: {pr_comments[:80]}")

    # Hard fail — the deletion-test property. If the diff contains the
    # reachable ``raise NotImplementedError`` AND no layer fired, the
    # no-defer stack has been gutted (or it never ran).
    pr_merged = bool((data.get("pr_review") or {}).get("merged"))
    if pr_merged and "raise NotImplementedError" in diff:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                "DELETION TEST FAILED: PR merged with reachable "
                "`raise NotImplementedError` in diff. No no-defer layer "
                "blocked. " + "; ".join(reasons)
            ),
        }

    score = min(1.0, 0.25 * len(layers_fired))
    if layers_fired:
        return {
            "pass": True,
            "score": round(score, 2),
            "reason": (
                f"{len(layers_fired)} no-defer layer(s) fired: "
                f"{', '.join(layers_fired)} — " + "; ".join(reasons)
            ),
        }

    return {
        "pass": False,
        "score": 0.0,
        "reason": (
            "No no-defer layer fired. The deferred-stub task slipped through. "
            "Reasons collected: " + ("; ".join(reasons) or "(none)")
        ),
    }
