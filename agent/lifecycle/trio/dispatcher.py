"""Per-backlog-item dispatcher — coder/reviewer subagents in the parent's slot.

The trio orchestrator (``run_trio_parent``) drains a backlog of work
items the architect produced. ADR-013 replaces the legacy
``scheduler.dispatch_next`` / ``await_child`` pair (which materialised
each backlog item as a top-level ``Task`` row and went through the
global concurrency queue) with this in-process dispatcher.

For one item, the loop is:

1. Snapshot HEAD on the integration branch (``start_sha``).
2. Run a coder subagent (fresh ``AgentLoop``, full tools, integration
   branch checked out). It commits its work on top of ``start_sha``.
3. ``git diff start_sha..HEAD`` — the surface the reviewer judges.
4. Run a reviewer subagent (fresh ``AgentLoop``, ``readonly=True`` +
   ``with_browser=True``: ``file_read`` / ``grep`` / ``glob`` / ``git``
   / ``browse_url`` / ``skill``). It returns a verdict JSON block
   ``{"ok": bool, "feedback": str}``.
5. If ``ok`` — done. If not — feed the feedback back into the SAME
   coder on the next round (``resume=True``) so the coder can either
   fix or push back. Cap at ``MAX_ROUNDS`` round-trips per item.
6. If we exhaust the cap without converging, the caller can ask the
   architect for a tiebreak.

The dispatcher itself doesn't touch the DB or call the architect —
it returns an ``ItemResult`` and lets the caller persist + decide.
This keeps the module pure-ish so tests can exercise the
coder↔reviewer state machine without a Postgres fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from agent import sh
from agent.lifecycle._naming import _fresh_session_id
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.trio.reviewer import _extract_verdict

log = structlog.get_logger()


# Per-item cap on coder→reviewer round-trips before escalating to the
# architect tiebreak. Module-level so tests can shrink it.
MAX_ROUNDS = 3


@dataclass
class TranscriptEntry:
    role: str  # "coder" or "reviewer"
    round: int  # 1-indexed
    output: str
    tool_calls: list[Any] = field(default_factory=list)
    verdict: dict | None = None  # reviewer only


@dataclass
class ItemResult:
    """Outcome of running one backlog item.

    - ``ok=True`` means the coder/reviewer pair converged on the item.
    - ``ok=False`` with ``needs_tiebreak=True`` means MAX_ROUNDS was
      exhausted without convergence; caller should ask the architect.
    - ``ok=False`` with ``needs_tiebreak=False`` means a terminal
      failure (e.g. coder produced no diff at all) and the caller
      should treat the item as blocked.
    """

    ok: bool
    transcript: list[TranscriptEntry]
    start_sha: str
    head_sha: str | None = None
    needs_tiebreak: bool = False
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


async def _git_head_sha(workspace: str) -> str:
    res = await sh.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, timeout=10,
    )
    if res.failed:
        raise RuntimeError(f"git rev-parse HEAD failed: {res.stderr}")
    return (res.stdout or "").strip()


async def _git_diff_since(workspace: str, start_sha: str) -> str:
    """Return the unified diff from ``start_sha`` to current HEAD."""
    res = await sh.run(
        ["git", "diff", f"{start_sha}..HEAD"],
        cwd=workspace, timeout=30, max_output=200_000,
    )
    if res.failed:
        log.warning(
            "trio.dispatcher.git_diff_failed",
            start_sha=start_sha,
            stderr=(res.stderr or "")[:300],
        )
        return ""
    return res.stdout or ""


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _coder_prompt(work_item: dict, *, prior_feedback: str | None) -> str:
    """Build the coder's first-turn prompt."""
    title = work_item.get("title", "(no title)")
    description = work_item.get("description", "")
    item_id = work_item.get("id", "")

    parts = [
        "You are implementing one work item from a larger architecture pass.",
        "ARCHITECTURE.md in the workspace describes the overall design.",
        "",
        f"## Work item — {item_id}",
        f"**{title}**",
        "",
        description,
        "",
        "## Rules",
        "- Make the change on the current branch (already checked out — do not switch branches).",
        "- Commit your work locally when done (one or more commits is fine).",
        "- Do NOT push, do NOT open a PR — the trio parent handles integration.",
        "- Stay in scope. Other backlog items will be implemented by other coders.",
        "- When done, summarise what you did and why in your final message so the reviewer can read it.",
    ]
    if prior_feedback:
        parts += [
            "",
            "## Prior reviewer feedback (you can fix this OR push back if you disagree)",
            prior_feedback,
            "",
            "If you disagree with the reviewer, explain why in your final message — "
            "the spec wins over a reviewer's preference. If you agree, fix and "
            "re-summarise.",
        ]
    return "\n".join(parts)


def _reviewer_prompt(
    work_item: dict, *, coder_summary: str, diff: str
) -> str:
    """Build the reviewer's prompt."""
    title = work_item.get("title", "(no title)")
    description = work_item.get("description", "")
    item_id = work_item.get("id", "")

    # Cap diff in the prompt — reviewer can re-run `git diff` if it needs
    # more context, but we don't want to blast 200 KB into the model.
    diff_preview = diff if len(diff) < 30_000 else diff[:30_000] + "\n... (truncated)"

    return "\n".join(
        [
            "You are reviewing one builder cycle in a trio (architect/coder/reviewer).",
            "Your job: does the coder's change satisfy the work item, given the architecture?",
            "",
            f"## Work item — {item_id}",
            f"**{title}**",
            "",
            description,
            "",
            "## Coder's summary of what they did",
            coder_summary or "(no summary)",
            "",
            "## Diff to review (snapshot — use `git diff` if you need more)",
            "```diff",
            diff_preview,
            "```",
            "",
            "## Rules",
            "- You may read files (`file_read`, `grep`, `glob`), run `git diff/log/show`, ",
            "  and `browse_url` to verify UI changes against the spec.",
            "- You cannot edit files, run shell commands, or run tests — speak via the verdict.",
            "- The spec is authoritative. Don't reject for things the work item didn't ask for.",
            "- If the coder pushed back in their summary, weigh their argument seriously.",
            "",
            "## Output",
            "End your message with a verdict block:",
            "```json",
            '{"ok": <true|false>, "feedback": "<actionable, specific>"}',
            "```",
        ]
    )


def _tiebreak_prompt(work_item: dict, transcript: list[TranscriptEntry]) -> str:
    """Architect tiebreak prompt — full transcript + decision schema."""
    title = work_item.get("title", "(no title)")
    item_id = work_item.get("id", "")

    transcript_str = []
    for e in transcript:
        transcript_str.append(f"### {e.role.upper()} (round {e.round})")
        transcript_str.append(e.output[:4000])
        if e.verdict is not None:
            transcript_str.append(
                f"Verdict: ok={e.verdict.get('ok')}, "
                f"feedback={e.verdict.get('feedback', '')[:500]}"
            )
        transcript_str.append("")

    return "\n".join(
        [
            f"## Tiebreak — work item {item_id}: {title}",
            "",
            "The coder and reviewer ran 3 rounds without converging. As the",
            "architect, you decide. Read the full transcript and pick:",
            "",
            "- `accept` — coder is right; mark this item done and continue.",
            "- `redo` — reviewer is right; specify the fix and re-dispatch the coder.",
            "- `revise_backlog` — the item itself is wrong; describe how to split,",
            "  merge, or reword it.",
            "- `clarify` — we need human input to break the tie; supply the question.",
            "",
            "## Transcript",
            "",
            "\n".join(transcript_str),
            "",
            "## Output",
            "End your message with a decision block:",
            "```json",
            '{"action": "accept|redo|revise_backlog|clarify",',
            ' "reason": "<one short sentence>",',
            ' "guidance": "<for `redo` — the specific fix to apply>",',
            ' "new_items": [{"id": "...", "title": "...", "description": "..."}],',
            ' "question": "<for `clarify` — what to ask the human>"}',
            "```",
        ]
    )


# ---------------------------------------------------------------------------
# Subagent runners
# ---------------------------------------------------------------------------


def _result_output(result: Any) -> str:
    if hasattr(result, "output"):
        return result.output or ""
    return str(result) if result is not None else ""


def _result_tool_calls(result: Any, agent: Any) -> list:
    for src in (agent, result):
        log_attr = getattr(src, "tool_call_log", None)
        if isinstance(log_attr, list):
            return log_attr
    calls = getattr(result, "tool_calls", None)
    if isinstance(calls, list):
        return calls
    return []


async def _run_coder(
    *,
    parent_task_id: int,
    work_item: dict,
    workspace: str,
    repo_name: str | None,
    home_dir: str | None,
    org_id: int | None,
    round_idx: int,
    prior_feedback: str | None,
) -> TranscriptEntry:
    """Spawn one coder subagent. Returns its transcript entry."""
    item_id = work_item.get("id", "item")
    session_id = _fresh_session_id(parent_task_id, f"coder-{item_id}-r{round_idx}")
    agent = create_agent(
        workspace=workspace,
        session_id=session_id,
        task_id=parent_task_id,
        task_description=work_item.get("description") or work_item.get("title", ""),
        with_browser=True,
        max_turns=40,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    prompt = _coder_prompt(work_item, prior_feedback=prior_feedback)
    run_result = await agent.run(prompt)
    return TranscriptEntry(
        role="coder",
        round=round_idx,
        output=_result_output(run_result),
        tool_calls=_result_tool_calls(run_result, agent),
    )


async def _run_reviewer(
    *,
    parent_task_id: int,
    work_item: dict,
    workspace: str,
    repo_name: str | None,
    home_dir: str | None,
    org_id: int | None,
    round_idx: int,
    coder_summary: str,
    diff: str,
) -> TranscriptEntry:
    """Spawn one reviewer subagent. Returns its transcript entry with verdict."""
    item_id = work_item.get("id", "item")
    session_id = _fresh_session_id(parent_task_id, f"reviewer-{item_id}-r{round_idx}")
    agent = create_agent(
        workspace=workspace,
        session_id=session_id,
        task_id=parent_task_id,
        task_description=work_item.get("description") or work_item.get("title", ""),
        readonly=True,
        with_browser=True,
        max_turns=20,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    prompt = _reviewer_prompt(work_item, coder_summary=coder_summary, diff=diff)
    run_result = await agent.run(prompt)
    output = _result_output(run_result)
    return TranscriptEntry(
        role="reviewer",
        round=round_idx,
        output=output,
        tool_calls=_result_tool_calls(run_result, agent),
        verdict=_extract_verdict(output),
    )


# ---------------------------------------------------------------------------
# Public entry point — dispatch one item
# ---------------------------------------------------------------------------


async def dispatch_item(
    *,
    parent_task_id: int,
    work_item: dict,
    workspace: str,
    repo_name: str | None,
    home_dir: str | None,
    org_id: int | None,
) -> ItemResult:
    """Run one backlog item through up-to-``MAX_ROUNDS`` coder↔reviewer rounds.

    Returns an ``ItemResult``. The caller is responsible for persisting
    backlog updates and invoking the architect tiebreak when
    ``needs_tiebreak`` is true.
    """
    start_sha = await _git_head_sha(workspace)
    transcript: list[TranscriptEntry] = []
    feedback: str | None = None

    for round_idx in range(1, MAX_ROUNDS + 1):
        coder_entry = await _run_coder(
            parent_task_id=parent_task_id,
            work_item=work_item,
            workspace=workspace,
            repo_name=repo_name,
            home_dir=home_dir,
            org_id=org_id,
            round_idx=round_idx,
            prior_feedback=feedback,
        )
        transcript.append(coder_entry)

        diff = await _git_diff_since(workspace, start_sha)
        if not diff.strip():
            # Coder made no observable changes at all — nothing to review.
            # Try one more round with explicit feedback, but if this is
            # already round MAX_ROUNDS we bail without a tiebreak (the
            # architect can't break a tie with no actual work to judge).
            log.warning(
                "trio.dispatcher.coder_produced_no_diff",
                parent_id=parent_task_id,
                item_id=work_item.get("id"),
                round=round_idx,
            )
            if round_idx >= MAX_ROUNDS:
                return ItemResult(
                    ok=False,
                    transcript=transcript,
                    start_sha=start_sha,
                    head_sha=start_sha,
                    needs_tiebreak=False,
                    failure_reason="coder_produced_no_diff",
                )
            feedback = (
                "You did not actually change any files. Read the work item "
                "again and make the concrete edits needed to implement it."
            )
            continue

        reviewer_entry = await _run_reviewer(
            parent_task_id=parent_task_id,
            work_item=work_item,
            workspace=workspace,
            repo_name=repo_name,
            home_dir=home_dir,
            org_id=org_id,
            round_idx=round_idx,
            coder_summary=coder_entry.output,
            diff=diff,
        )
        transcript.append(reviewer_entry)

        verdict = reviewer_entry.verdict
        if verdict is not None and verdict.get("ok"):
            head_sha = await _git_head_sha(workspace)
            return ItemResult(
                ok=True,
                transcript=transcript,
                start_sha=start_sha,
                head_sha=head_sha,
            )

        # Reject (or invalid JSON treated as reject). Feed feedback back
        # into the coder on the next round.
        if verdict is None:
            feedback = (
                "Reviewer returned no valid verdict JSON. Please clearly "
                "restate what you did and what assumptions you made."
            )
        else:
            feedback = str(verdict.get("feedback", "")) or "Reviewer rejected without specifics."

    # MAX_ROUNDS exhausted — escalate to architect tiebreak.
    head_sha = await _git_head_sha(workspace)
    return ItemResult(
        ok=False,
        transcript=transcript,
        start_sha=start_sha,
        head_sha=head_sha,
        needs_tiebreak=True,
    )


# ---------------------------------------------------------------------------
# Architect tiebreak — invoked by the caller when needs_tiebreak is True
# ---------------------------------------------------------------------------


async def architect_tiebreak(
    *,
    parent_task_id: int,
    work_item: dict,
    transcript: list[TranscriptEntry],
    workspace: str,
    repo_name: str | None,
    home_dir: str | None,
    org_id: int | None,
) -> dict:
    """Run the architect agent over a stuck coder↔reviewer transcript.

    Returns a decision dict with at minimum ``action`` ∈ {accept, redo,
    revise_backlog, clarify}. The caller acts on the decision (mutates
    ``trio_backlog``, dispatches a fresh coder, emits a clarification,
    etc.).
    """
    # Avoid circular import — architect.py imports nothing from dispatcher.
    import json

    from agent.lifecycle.trio.architect import (
        _JSON_BLOCK_RE,
        create_architect_agent,
    )

    agent = create_architect_agent(
        workspace=workspace,
        task_id=parent_task_id,
        task_description=work_item.get("description") or work_item.get("title", ""),
        phase="checkpoint",  # checkpoint system prompt is the closest fit
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    prompt = _tiebreak_prompt(work_item, transcript)
    run_result = await agent.run(prompt)
    output = _result_output(run_result)

    decision: dict = {}
    matches = list(_JSON_BLOCK_RE.finditer(output))
    for m in reversed(matches):
        try:
            candidate = json.loads(m.group(1))
            if isinstance(candidate, dict) and "action" in candidate:
                decision = candidate
                break
        except json.JSONDecodeError:
            continue

    if not decision:
        # Fail-safe: if the architect produced no parseable decision,
        # treat as "clarify" so a human breaks the tie.
        log.warning(
            "trio.dispatcher.tiebreak_no_decision",
            parent_id=parent_task_id,
            item_id=work_item.get("id"),
        )
        decision = {
            "action": "clarify",
            "reason": "architect produced no parseable decision JSON",
            "question": (
                f"The trio could not converge on work item "
                f"{work_item.get('id')}. Please review the transcript and "
                "decide whether to accept the coder's work or rewrite the item."
            ),
        }
    return decision


__all__ = [
    "MAX_ROUNDS",
    "ItemResult",
    "TranscriptEntry",
    "architect_tiebreak",
    "dispatch_item",
]
