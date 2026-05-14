"""complex_large final reviewer — ADR-015 §4 / Phase 7.

After the per-item builder/heavy-review loop drains, the orchestrator
dispatches ONE final-reviewer agent over the integrated diff. Its job
is to look at the whole change end-to-end and decide whether the design
doc's goals are met. Context shipped explicitly into the prompt:

  - ``.auto-agent/design.md`` — the architect's design.
  - All per-item ``.auto-agent/reviews/<id>.json`` verdict files.
  - The integrated unified diff (HEAD vs base branch).
  - The original grill output, if any.
  - On gap-fix rounds 2/3: the previous gap list + a one-paragraph
    summary of the previous attempt, attached as explicit prompt context
    (final reviewer has no persisted session — fresh each round).

The reviewer screenshots + smokes the union of every item's
``affected_routes``, then writes ``.auto-agent/final_review.json`` via
the ``submit-final-review`` skill. Verdict is ``"passed"`` (handed off
to PR creation) or ``"gaps_found"`` (bounces back to the architect's
persisted session for one round of new backlog items — see
:mod:`agent.lifecycle.trio.gap_fix`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from agent import sh
from agent.lifecycle.factory import create_agent
from agent.lifecycle.route_inference import is_ui_route
from agent.lifecycle.verify_primitives import (
    ServerHandle,
    boot_dev_server,
    exercise_routes,
    inspect_ui,
)
from agent.lifecycle.workspace_paths import (
    AUTO_AGENT_DIR,
    BACKLOG_PATH,
    DESIGN_PATH,
    FINAL_REVIEW_PATH,
    slice_backlog_path,
    slice_design_path,
    slice_dir,
    slice_reviews_dir,
)
from agent.lifecycle.workspace_reader import read_gate_file

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FinalReviewResult:
    verdict: str  # "passed" or "gaps_found"
    gaps: list[dict[str, Any]] = field(default_factory=list)
    comments: str = ""


# Two-attempt budget for missing-output retry, per ADR-015 §12.
_MAX_MISSING_OUTPUT_RETRIES = 2


# ---------------------------------------------------------------------------
# Loaders for the context the agent needs.
# ---------------------------------------------------------------------------


async def _load_integrated_diff(workspace_root: str, *, base_branch: str) -> str:
    """Return the unified diff from ``base_branch`` to HEAD."""

    res = await sh.run(
        ["git", "diff", f"{base_branch}...HEAD"],
        cwd=workspace_root,
        timeout=30,
        max_output=400_000,
    )
    if not res.failed and (res.stdout or "").strip():
        return res.stdout
    return ""


def _read_design(workspace_root: str, *, slice_name: str | None = None) -> str:
    rel = slice_design_path(slice_name) if slice_name else DESIGN_PATH
    text = read_gate_file(workspace_root, rel, schema_version="1")
    if isinstance(text, str):
        return text
    return ""


def _read_backlog_items(
    workspace_root: str,
    *,
    slice_name: str | None = None,
) -> list[dict[str, Any]]:
    """Read the backlog file and return its items, or empty list when missing."""

    rel = slice_backlog_path(slice_name) if slice_name else BACKLOG_PATH
    payload = read_gate_file(workspace_root, rel, schema_version="1")
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    return []


def _read_reviews(
    workspace_root: str,
    *,
    slice_name: str | None = None,
) -> dict[str, dict]:
    """Walk reviews directory and return ``{item_id: review_dict}``.

    Root reviews live at ``.auto-agent/reviews/``; slice reviews at
    ``.auto-agent/slices/<name>/reviews/``.
    """

    out: dict[str, dict] = {}
    if slice_name:
        directory = Path(workspace_root) / slice_reviews_dir(slice_name)
    else:
        directory = Path(workspace_root) / AUTO_AGENT_DIR / "reviews"
    if not directory.is_dir():
        return out
    for entry in sorted(directory.iterdir()):
        if entry.suffix != ".json":
            continue
        try:
            with open(entry) as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            out[entry.stem] = payload
    return out


def _union_affected_routes(items: list[dict]) -> list[str]:
    routes: list[str] = []
    seen: set[str] = set()
    for item in items:
        for r in item.get("affected_routes") or []:
            if isinstance(r, str) and r and r not in seen:
                routes.append(r)
                seen.add(r)
    return routes


# ---------------------------------------------------------------------------
# Smoke + UI primitive composition.
# ---------------------------------------------------------------------------


async def _smoke_and_ui(
    *,
    workspace_root: str,
    routes: list[str],
    intent: str,
) -> tuple[list[dict[str, Any]], str]:
    """Boot, exercise, and UI-inspect ``routes``.

    Returns ``(gaps, smoke_summary)``. Each gap dict has shape
    ``{"description": str, "affected_routes": [str]}``.
    """

    gaps: list[dict[str, Any]] = []
    if not routes:
        return gaps, "no routes to smoke"

    handle: ServerHandle | None = None
    try:
        handle = await boot_dev_server(workspace=workspace_root)
        if handle.state == "failed":
            gaps.append(
                {
                    "description": (
                        f"dev server boot failed: {handle.failure_reason}; cannot smoke {routes!r}."
                    ),
                    "affected_routes": list(routes),
                }
            )
            return gaps, f"boot_failed: {handle.failure_reason}"
        if handle.state == "disabled":
            return gaps, "smoke: skipped (no boot config)"

        route_results = await exercise_routes(routes, handle=handle)
        for route, rr in route_results.items():
            if rr.ok:
                continue
            gaps.append(
                {
                    "description": (
                        f"route {route} returned status={rr.status}, "
                        f"reason={rr.reason!r}; expected 2xx with non-stub body."
                    ),
                    "affected_routes": [route],
                }
            )

        # UI inspection — only on routes that returned 2xx (re-runs after).
        for route, rr in route_results.items():
            if not rr.ok or not is_ui_route(route):
                continue
            ui = await inspect_ui(route=route, intent=intent, base_url=handle.base_url)
            if not ui.ok and "playwright_not_installed" not in (ui.reason or ""):
                gaps.append(
                    {
                        "description": f"UI inspection failed for {route}: {ui.reason}",
                        "affected_routes": [route],
                    }
                )

        smoke_summary = (
            f"smoke: {len(route_results)} routes exercised; "
            f"{sum(1 for r in route_results.values() if not r.ok)} failures"
        )
        return gaps, smoke_summary
    finally:
        if handle is not None:
            await handle.teardown()


# ---------------------------------------------------------------------------
# Final-review prompt for the LLM agent that calls submit-final-review.
# ---------------------------------------------------------------------------


_FINAL_REVIEW_PROMPT = """\
You are the final reviewer for a complex_large run. The per-item
builder/review loop has finished; every item shipped with a passing
heavy-review. Your job: look at the whole change end-to-end and decide
whether the original design's goals are met or whether gaps remain.

**No-defer rule (Rules — ADR-015 §8):**

> Treat any `raise NotImplementedError`, `# TODO(phase`, `# Phase 1` /
> `# Phase-1` / `# Phase 1:` (or any variant), 'Phase 1 fills this in',
> `# v2 will`, `# in a future PR`, 'will be implemented later', 'for
> now this is a stub', or equivalent in the integrated diff as a GAP.
> Emit a `gaps_found` verdict so the architect closes them in the next
> round — never approve a run with deferred work. If a stub is genuinely
> warranted (e.g. an abstract base-class method), it must carry
> `# auto-agent: allow-stub` and explain why; in that case it is not a
> gap.

Workspace context attached below:
  - The architect's design doc (the single approval artefact).
  - Every per-item review verdict.
  - The integrated unified diff (HEAD vs base).
  - The original grill output (if any).
  - On gap-fix rounds 2+, the previous gap list + previous-attempt
    summary (no persisted session — read these explicitly).

You receive the smoke + UI report from the orchestrator's verify
primitives as additional input. If the report contains failures, treat
them as gaps; you may also add gaps based on reading the diff (e.g. a
missing test, a hardcoded credential, an integration the design called
for but the diff doesn't cover).

Output your verdict by calling the ``submit-final-review`` skill — it
writes ``.auto-agent/final_review.json``. Schema:

  - ``verdict: "passed"`` with empty ``gaps: []`` when nothing remains.
  - ``verdict: "gaps_found"`` with a list of gaps; each gap has
    ``description`` (one paragraph) and ``affected_routes`` (a list,
    may be empty).

==== Design doc ====
{design}

==== Per-item reviews ====
{reviews}

==== Smoke + UI primitive report ====
{smoke_report}

==== Integrated diff ====
{diff}

==== Original grill output ====
{grill}

{previous_round_block}
"""


_PREVIOUS_ROUND_TEMPLATE = """\
==== Previous gap-fix round ({round_idx}/3) — context only ====

Previous gaps that were supposed to be closed:
{previous_gaps}

Previous attempt summary:
{previous_attempt_summary}

You are running fresh; the architect produced new items to close these
gaps, and the per-item review loop just ran again. Verify that the new
gaps are not the same ones, and that the original ones are closed.
"""


def _render_reviews(reviews: dict[str, dict]) -> str:
    if not reviews:
        return "(no review files found)"
    lines: list[str] = []
    for item_id, payload in reviews.items():
        verdict = payload.get("verdict", "?")
        reason = payload.get("reason", "")[:300]
        lines.append(f"- {item_id}: verdict={verdict} — {reason}")
    return "\n".join(lines)


def _render_smoke(gaps: list[dict], smoke_summary: str) -> str:
    if not gaps:
        return f"{smoke_summary}; no failures detected."
    lines = [smoke_summary, ""]
    for g in gaps:
        lines.append(f"- {g.get('description', '')} (routes: {g.get('affected_routes') or []!r})")
    return "\n".join(lines)


def _render_previous_gaps(previous_gaps: list[dict] | None) -> str:
    if not previous_gaps:
        return "(none)"
    lines = []
    for g in previous_gaps:
        lines.append(f"- {g.get('description', '')} (routes: {g.get('affected_routes') or []!r})")
    return "\n".join(lines)


async def _run_final_review_agent(
    *,
    parent_task_id: int,
    workspace_root: str,
    prompt: str,
    repo_name: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
) -> str:
    """Run the LLM agent that calls the submit-final-review skill.

    Returns the agent's text output. The orchestrator reads
    ``final_review.json`` after this returns — see ``run_final_review``.
    """

    agent = create_agent(
        workspace=workspace_root,
        task_id=parent_task_id,
        task_description=prompt[:200],
        readonly=False,
        with_browser=True,
        max_turns=20,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
        session=None,  # FRESH each round — ADR-015 §4
    )
    result = await agent.run(prompt)
    return getattr(result, "output", None) or ""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def _final_review_rel_path(slice_name: str | None) -> str:
    """Where the final-review verdict file lives — root or slice-scoped."""

    if slice_name:
        return f"{slice_dir(slice_name)}/final_review.json"
    return FINAL_REVIEW_PATH


def _write_final_review_json(
    workspace_root: str,
    result: FinalReviewResult,
    *,
    slice_name: str | None = None,
) -> None:
    rel = _final_review_rel_path(slice_name)
    abs_path = os.path.join(workspace_root, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    payload = {
        "schema_version": "1",
        "verdict": result.verdict,
        "gaps": result.gaps,
        "comments": result.comments,
    }
    with open(abs_path, "w") as fh:
        json.dump(payload, fh, indent=2)


async def run_final_review(
    *,
    workspace_root: str,
    parent_task_id: int,
    grill_output: str = "",
    base_branch: str = "main",
    previous_gaps: list[dict] | None = None,
    previous_attempt_summary: str = "",
    repo_name: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
    slice_name: str | None = None,
) -> FinalReviewResult:
    """Run the final reviewer for one round.

    Steps:
      1. Compose context: design.md, reviews, diff, grill output, previous
         round summary (if any).
      2. Smoke + UI the union of all items' affected_routes via the verify
         primitives — synthesises gaps when routes fail.
      3. Hand the composed prompt to the LLM agent and ask it to write
         ``final_review.json`` via the ``submit-final-review`` skill.
      4. Read the file, validate, return.

    If the file is missing after the agent's turn, retry once (per
    ADR-015 §12). If smoke produced gaps but the agent says ``passed``,
    we override with the smoke gaps — primitives are ground truth.

    ``slice_name`` makes the reviewer slice-scoped — design.md / backlog /
    reviews / final_review.json all read and write under
    ``.auto-agent/slices/<name>/`` (ADR-015 §9). When ``None``, the root
    namespace is used.
    """

    design = _read_design(workspace_root, slice_name=slice_name)
    items = _read_backlog_items(workspace_root, slice_name=slice_name)
    reviews = _read_reviews(workspace_root, slice_name=slice_name)
    diff = await _load_integrated_diff(workspace_root, base_branch=base_branch)
    union_routes = _union_affected_routes(items)
    intent = design or "complex_large run"

    smoke_gaps, smoke_summary = await _smoke_and_ui(
        workspace_root=workspace_root,
        routes=union_routes,
        intent=intent[:200],
    )

    # Round-aware prompt context.
    round_idx = 1
    if previous_gaps:
        round_idx = 2 if not previous_attempt_summary else round_idx
    previous_block = ""
    if previous_gaps or previous_attempt_summary:
        previous_block = _PREVIOUS_ROUND_TEMPLATE.format(
            round_idx=round_idx,
            previous_gaps=_render_previous_gaps(previous_gaps),
            previous_attempt_summary=previous_attempt_summary or "(none)",
        )

    prompt = _FINAL_REVIEW_PROMPT.format(
        design=(design or "(no design.md found)")[:8000],
        reviews=_render_reviews(reviews),
        smoke_report=_render_smoke(smoke_gaps, smoke_summary),
        diff=(diff or "(empty)")[:40_000],
        grill=(grill_output or "(none)")[:4000],
        previous_round_block=previous_block,
    )

    # Clear any stale final_review.json so a previous round's verdict
    # doesn't accidentally satisfy this turn.
    review_rel = _final_review_rel_path(slice_name)
    final_path = os.path.join(workspace_root, review_rel)
    if os.path.isfile(final_path):
        os.remove(final_path)

    last_output = ""
    for attempt in range(_MAX_MISSING_OUTPUT_RETRIES):
        amend = ""
        if attempt > 0:
            amend = (
                "Your previous response did not write final_review.json. "
                "You MUST call the submit-final-review skill before stopping.\n\n"
            )
        last_output = await _run_final_review_agent(
            parent_task_id=parent_task_id,
            workspace_root=workspace_root,
            prompt=amend + prompt,
            repo_name=repo_name,
            home_dir=home_dir,
            org_id=org_id,
        )

        payload = read_gate_file(workspace_root, review_rel, schema_version="1")
        if isinstance(payload, dict):
            verdict = payload.get("verdict")
            agent_gaps = list(payload.get("gaps") or [])
            comments = str(payload.get("comments") or "")

            # Compose: agent gaps union smoke gaps. Primitives are ground
            # truth — if smoke caught a failure the agent missed, surface it.
            merged_gaps = list(agent_gaps)
            for sg in smoke_gaps:
                if sg not in merged_gaps:
                    merged_gaps.append(sg)
            if verdict not in ("passed", "gaps_found"):
                log.warning("final_review.bad_verdict", attempt=attempt, verdict=verdict)
                continue

            final_verdict = "gaps_found" if merged_gaps else "passed"
            result = FinalReviewResult(
                verdict=final_verdict,
                gaps=merged_gaps,
                comments=comments,
            )
            _write_final_review_json(workspace_root, result, slice_name=slice_name)
            return result
        log.warning(
            "final_review.missing_output",
            attempt=attempt,
            preview=last_output[:200],
        )

    # Both attempts missed — synthesise from smoke gaps alone so the run
    # can advance (or escalate).
    final_verdict = "gaps_found" if smoke_gaps else "passed"
    result = FinalReviewResult(
        verdict=final_verdict,
        gaps=smoke_gaps,
        comments=(
            f"agent did not write {review_rel} after "
            f"{_MAX_MISSING_OUTPUT_RETRIES} attempts; using smoke-only verdict."
        ),
    )
    _write_final_review_json(workspace_root, result, slice_name=slice_name)
    return result


__all__ = [
    "FinalReviewResult",
    "run_final_review",
]
