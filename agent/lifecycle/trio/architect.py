"""Architect agent for the trio lifecycle.

Four phases: initial, consult, checkpoint, revision. Each persists an
``ArchitectAttempt`` row scoped to the trio parent task.
"""
from __future__ import annotations

from typing import Literal

from agent.lifecycle.factory import create_agent
from agent.lifecycle.trio.prompts import (
    ARCHITECT_CHECKPOINT_SYSTEM,
    ARCHITECT_CONSULT_SYSTEM,
    ARCHITECT_INITIAL_SYSTEM,
)

_SYSTEM_PROMPTS = {
    "initial":    ARCHITECT_INITIAL_SYSTEM,
    "consult":    ARCHITECT_CONSULT_SYSTEM,
    "checkpoint": ARCHITECT_CHECKPOINT_SYSTEM,
    "revision":   ARCHITECT_INITIAL_SYSTEM,  # same shape as initial
}


def create_architect_agent(
    workspace: str,
    task_id: int,
    task_description: str,
    phase: Literal["initial", "consult", "checkpoint", "revision"],
    repo_name: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
):
    """Build an AgentLoop configured for the architect.

    The architect always has:
    - web_search + fetch_url (outside grounding)
    - record_decision + request_market_brief (its bespoke tools)
    - Standard file/bash/git tools
    """
    agent = create_agent(
        workspace=workspace,
        task_id=task_id,
        task_description=task_description,
        with_web=True,
        max_turns=80,
        include_methodology=False,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    # Replace the default registry with one that adds the architect-only tools.
    from agent.tools import create_default_registry

    agent.tools = create_default_registry(
        with_web=True,
        with_architect_tools=True,
    )
    # Inject the phase-specific system prompt.
    agent.system_prompt_override = _SYSTEM_PROMPTS[phase]
    return agent


# Forward declarations — implemented in subsequent tasks.
async def run_initial(parent_task_id: int):
    raise NotImplementedError("Task 12")


async def consult(*, parent_task_id: int, child_task_id: int, question: str, why: str):
    raise NotImplementedError("Task 13")


async def checkpoint(
    parent_task_id: int,
    *,
    child_task_id: int | None = None,
    repair_context: dict | None = None,
):
    raise NotImplementedError("Task 14")


async def run_revision(parent_task_id: int):
    raise NotImplementedError("Task 14")
