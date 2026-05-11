"""Planning phase — runs the agent in readonly mode to produce a plan.

Three entry paths within ``handle_planning``:
  1. Plan-revision (user rejected previous plan, sent feedback).
  2. Grill phase (complex task, grilling not complete) — ask one question at
     a time until the agent emits ``GRILL_DONE``.
  3. Final plan (simple task, OR grilling done).

State is held in the module-level ``_active_planning`` set so concurrent
re-entries on the same task_id (from duplicate events or rapid user replies)
collapse onto a single in-flight planning run.
"""

from __future__ import annotations

import re as _re
from datetime import UTC, datetime, timedelta

import httpx

from agent.lifecycle._clarification import _extract_clarification
from agent.lifecycle._naming import _session_id
from agent.lifecycle._orchestrator_api import (
    ORCHESTRATOR_URL,
    get_repo,
    get_task,
    transition_task,
)
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.prompts import (
    GRILL_DONE_MARKER,
    GRILL_DONE_QUESTION_SENTINEL,
    build_grill_phase_prompt,
    build_planning_prompt,
)
from agent.workspace import cleanup_workspace, clone_repo
from shared.events import (
    Event,
    publish,
    repo_onboard,
    task_clarification_needed,
    task_plan_ready,
)
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.planning")


SUMMARY_MAX_AGE = timedelta(days=7)

# Tasks where grilling is skipped — the agent goes straight to planning.
# Simple tasks don't need design alignment; query/no-code tasks aren't planned;
# tasks created from architecture-mode suggestions arrive pre-grilled
# (intake_qa = []).
_SKIP_GRILL_COMPLEXITIES = {"simple", "simple_no_code"}

# Hard cap on grill rounds to bound user fatigue. The grill prompt asks the
# agent to aim for 3–7 questions and emit GRILL_DONE; this is a fail-safe in
# case the agent keeps asking without ever exiting. Counted as
# "non-sentinel entries in intake_qa" — when the count reaches this limit on
# entry to handle_planning, we force a synthetic GRILL_DONE and proceed to
# planning with the transcript so far.
_MAX_GRILL_ROUNDS = 10

_PLAN_MAX_LENGTH = 50_000  # Must match TransitionRequest.plan max_length


_active_planning: set[int] = set()


def _extract_grill_done(output: str) -> str | None:
    """Check if agent output declares grilling complete (GRILL_DONE: <reason>)."""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(GRILL_DONE_MARKER):
            return stripped[len(GRILL_DONE_MARKER) :].strip() or "(no reason)"
    return None


def _should_run_grill(task) -> bool:
    """Decide whether the grill phase runs before planning for this task.

    Four signals on ``task.intake_qa`` drive the gate:
      - ``None`` → grilling never started. Run grill (initial turn).
      - ``[]`` → grilling explicitly skipped (simple tasks, architecture-
        derived tasks). Skip.
      - ``[…, {"question": GRILL_DONE_QUESTION_SENTINEL, …}]`` → grilling
        completed (sentinel appended after agent emitted GRILL_DONE). Skip.
      - ``[…, {"question": q, "answer": …}]`` (no sentinel) → grilling in
        progress. Run grill again so the agent can ask the next question
        OR emit GRILL_DONE.

    The ``_MAX_GRILL_ROUNDS`` cap is enforced inside ``handle_planning``
    (it forces a synthetic GRILL_DONE rather than mutating gate semantics).
    """
    if not task.complexity or task.complexity in _SKIP_GRILL_COMPLEXITIES:
        return False
    if task.intake_qa is None:
        return True
    if not task.intake_qa:
        return False  # Empty list = explicitly complete/skipped.
    return not any(qa.get("question") == GRILL_DONE_QUESTION_SENTINEL for qa in task.intake_qa)


def _grill_round_count(intake_qa: list[dict] | None) -> int:
    """Number of real grill rounds — sentinel entries don't count."""
    if not intake_qa:
        return 0
    return sum(1 for qa in intake_qa if qa.get("question") != GRILL_DONE_QUESTION_SENTINEL)


async def _save_intake_qa(task_id: int, intake_qa: list[dict]) -> None:
    """PATCH the task's intake_qa via the orchestrator API."""
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/intake_qa",
            json={"intake_qa": intake_qa},
        )
        resp.raise_for_status()


def _trim_plan_text(text: str) -> str:
    """Strip agent preamble from plan text and enforce the API size limit.

    The agent joins ALL assistant messages (including exploration/reasoning)
    which can exceed the 50KB API limit.  Strip everything before the first
    markdown heading, then hard-truncate if still too long.
    """
    heading = _re.search(r"^(#{1,3} )", text, _re.MULTILINE)
    if heading:
        text = text[heading.start() :]
    if len(text) > _PLAN_MAX_LENGTH:
        text = text[: _PLAN_MAX_LENGTH - 20] + "\n\n*(plan truncated)*"
    return text


async def generate_repo_summary(repo_url: str, repo_name: str, default_branch: str) -> str | None:
    """Generate a repo summary using the agent with readonly tools."""
    from agent.workspace import clone_repo as _clone

    workspace = await _clone(repo_url, 0, default_branch, workspace_name=f"summary-{repo_name}")
    agent = create_agent(workspace, readonly=True, max_turns=15, model_tier="fast")
    result = await agent.run(
        "Provide a concise summary of this repository: tech stack, project structure, "
        "key patterns, domain, and any notable conventions. Be brief (under 500 words)."
    )
    return result.output if result.output.strip() else None


async def handle_planning(task_id: int, feedback: str | None = None) -> None:
    """Run the agent in planning mode (readonly tools) for complex tasks."""
    if task_id in _active_planning:
        log.info(f"Task #{task_id} planning already active — skipping duplicate")
        return

    task = await get_task(task_id)
    if not task:
        return

    if not task.repo_name:
        await transition_task(task_id, "blocked", "No repo assigned to this task")
        return

    repo = await get_repo(task.repo_name)
    if not repo:
        await transition_task(task_id, "blocked", f"Repo '{task.repo_name}' not found")
        return

    # Trigger harness onboarding if not done yet
    if not repo.harness_onboarded:
        log.info(f"Repo '{repo.name}' not harness-onboarded, triggering onboarding")
        await publish(repo_onboard(repo_id=repo.id, repo_name=repo.name))

    # Generate repo summary if missing or stale
    summary_stale = False
    if repo.summary and repo.summary_updated_at:
        updated = repo.summary_updated_at
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        summary_stale = datetime.now(UTC) - updated > SUMMARY_MAX_AGE

    if not repo.summary or summary_stale:
        reason = "stale" if summary_stale else "missing"
        log.info(f"Generating summary for repo '{repo.name}' ({reason})...")
        try:
            summary = await generate_repo_summary(repo.url, repo.name, repo.default_branch)
            if summary:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{ORCHESTRATOR_URL}/repos/{repo.id}/summary",
                        json={"summary": summary},
                    )
                repo.summary = summary
                log.info(f"Summary generated for '{repo.name}' ({len(summary)} chars)")
        except Exception:
            log.exception(f"Failed to generate summary for '{repo.name}', continuing without")

    session_id = _session_id(task_id, task.created_at)
    log.info(f"Planning task #{task_id} in {task.repo_name} (session={session_id})")
    workspace = await clone_repo(repo.url, task_id, repo.default_branch)

    _active_planning.add(task_id)
    try:
        agent = create_agent(
            workspace,
            session_id=session_id,
            readonly=True,
            max_turns=30,
            include_methodology=True,
            task_description=task.description,
            repo_name=repo.name,
            home_dir=await home_dir_for_task(task),
        )

        # Track grill state in a local boolean. After GRILL_DONE we flip it
        # to False; we don't re-evaluate _should_run_grill(task) because the
        # local task object's intake_qa is stale (we patch via API).
        in_grill_phase = _should_run_grill(task) and not feedback

        # Three planning entry paths:
        #   1. Plan-revision (user rejected previous plan, sent feedback).
        #   2. Grill phase (complex task, grilling not complete) — ask one
        #      question at a time until the agent emits GRILL_DONE.
        #   3. Final plan (simple task, OR grilling done).
        if feedback:
            prompt = (
                f"The user rejected your previous plan with this feedback:\n\n{feedback}\n\n"
                f"Please revise the plan addressing their concerns. Output the revised plan as text."
            )
            result = await agent.run(prompt, resume=True)
        elif in_grill_phase:
            existing_qa = list(task.intake_qa or [])
            log.info(f"Task #{task_id} GRILL phase (intake_qa={len(existing_qa)} entries)")
            prompt = build_grill_phase_prompt(
                task.title,
                task.description,
                intake_qa=existing_qa,
                repo_summary=repo.summary,
            )
            result = await agent.run(prompt)
        else:
            prompt = build_planning_prompt(
                task.title,
                task.description,
                repo.summary,
                intake_qa=task.intake_qa,
            )
            result = await agent.run(prompt)

        # Plans can span multiple turns if they hit max_tokens and get
        # continuation prompts. Collect ALL assistant text to get the full plan.
        output = (
            "\n".join(
                msg.content for msg in result.messages if msg.role == "assistant" and msg.content
            )
            or result.output
        )

        # If we're in grill mode, GRILL_DONE switches us into a regular plan
        # right now (within the same handler invocation).
        if in_grill_phase:
            grill_done = _extract_grill_done(output)
            if grill_done is not None:
                log.info(f"Task #{task_id} GRILL_DONE: {grill_done[:120]}")
                # Persist the sentinel so future re-entry skips the grill
                # gate. Keep the existing transcript so the planner can use
                # it as preflight context.
                completed_qa = list(task.intake_qa or []) + [
                    {
                        "question": GRILL_DONE_QUESTION_SENTINEL,
                        "answer": grill_done[:500],
                    }
                ]
                await _save_intake_qa(task_id, completed_qa)
                task.intake_qa = completed_qa  # keep local consistent
                in_grill_phase = False

                # Re-run with the planning prompt, resuming the same session.
                plan_prompt = build_planning_prompt(
                    task.title,
                    task.description,
                    repo.summary,
                    intake_qa=completed_qa,
                )
                result = await agent.run(plan_prompt, resume=True)
                output = (
                    "\n".join(
                        msg.content
                        for msg in result.messages
                        if msg.role == "assistant" and msg.content
                    )
                    or result.output
                )

        # Check if agent needs clarification
        question = _extract_clarification(output)
        if question:
            phase = "grill" if in_grill_phase else "planning"

            # Hard cap on grill rounds — if the agent would push us past the
            # limit, force a synthetic GRILL_DONE and proceed to planning
            # with the transcript so far. This bounds user fatigue when the
            # agent can't decide it's heard enough.
            if phase == "grill" and _grill_round_count(task.intake_qa) >= _MAX_GRILL_ROUNDS:
                log.warning(
                    f"Task #{task_id} hit grill cap ({_MAX_GRILL_ROUNDS}); "
                    f"forcing GRILL_DONE and dropping question: {question[:80]}..."
                )
                completed_qa = list(task.intake_qa or []) + [
                    {
                        "question": GRILL_DONE_QUESTION_SENTINEL,
                        "answer": f"hit grill cap ({_MAX_GRILL_ROUNDS} rounds); proceeding to plan",
                    }
                ]
                await _save_intake_qa(task_id, completed_qa)
                task.intake_qa = completed_qa

                plan_prompt = build_planning_prompt(
                    task.title,
                    task.description,
                    repo.summary,
                    intake_qa=completed_qa,
                )
                result = await agent.run(plan_prompt, resume=True)
                output = (
                    "\n".join(
                        msg.content
                        for msg in result.messages
                        if msg.role == "assistant" and msg.content
                    )
                    or result.output
                )
                # Re-check for a (planning-phase) clarification on the new output
                question = _extract_clarification(output)
                phase = "planning"
                if not question:
                    output = _trim_plan_text(output)
                    # Fall through to the awaiting_approval transition below.

            if question:
                log.info(f"Task #{task_id} needs clarification ({phase}): {question[:100]}...")

                # Grill phase: append a {question, answer: None} to intake_qa so
                # the next turn (after the user replies) can fill in the answer.
                if phase == "grill":
                    intake_qa = list(task.intake_qa or [])
                    intake_qa.append({"question": question, "answer": None})
                    await _save_intake_qa(task_id, intake_qa)

                await transition_task(task_id, "awaiting_clarification", question)
                await publish(
                    task_clarification_needed(task_id, question=question, phase=phase)
                )
                return

        output = _trim_plan_text(output)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/tasks/{task_id}/transition",
                json={
                    "status": "awaiting_approval",
                    "message": "Plan ready for review",
                    "plan": output,
                },
            )
            resp.raise_for_status()

        await publish(task_plan_ready(task_id, plan=output))

    except Exception as e:
        log.exception(f"Planning failed for task #{task_id}")
        await transition_task(task_id, "failed", str(e))
        cleanup_workspace(task_id)
    finally:
        _active_planning.discard(task_id)


async def handle(event: Event) -> None:
    """EventBus entry — unpacks task.start_planning payload."""
    if not event.task_id:
        return
    feedback = event.payload.get("feedback") if event.payload else None
    await handle_planning(event.task_id, feedback=feedback)
