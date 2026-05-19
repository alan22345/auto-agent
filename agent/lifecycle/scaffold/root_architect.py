"""Phase B — root architect — ADR-018 §3.

One agent reads ``.auto-agent/intent.md`` and writes
``.auto-agent/adrs/000-system.md`` via the ``submit-root-adr`` skill.

The ADR is then structurally validated (``validate_root_adr``); on
failure we feed the errors back to the same agent for up to 2 retries
in-session. After that the caller's state machine takes over — the
parent transitions to AWAITING_ROOT_ADR_APPROVAL regardless of whether
the ADR is perfect; the user (or PO standin) gets the final say.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.scaffold.prompts import ROOT_ARCHITECT_SYSTEM
from agent.lifecycle.scaffold.validators import validate_root_adr
from agent.lifecycle.workspace_paths import INTENT_PATH, ROOT_ADR_PATH

if TYPE_CHECKING:
    from shared.models import Task

log = structlog.get_logger()


MAX_VALIDATION_RETRIES = 2


async def run(task: Task) -> str:
    """Run the root architect, returning the path to ``000-system.md``.

    Writes ``.auto-agent/adrs/000-system.md`` via the skill. On
    validation failure, replays the error back into the same agent
    session up to ``MAX_VALIDATION_RETRIES`` times. The orchestrator
    transitions the parent to AWAITING_ROOT_ADR_APPROVAL after this
    function returns, no matter the validation outcome — the gate is
    where humans get the final say.
    """

    workspace = await prepare_scaffold_workspace(task)
    home_dir = await home_dir_for_task(task)

    intent_path = os.path.join(workspace, INTENT_PATH)
    intent_text = ""
    if os.path.isfile(intent_path):
        try:
            with open(intent_path) as fh:
                intent_text = fh.read()
        except OSError:
            log.warning(
                "scaffold.root_architect.intent_read_failed",
                task_id=task.id,
                path=intent_path,
            )

    agent = create_agent(
        workspace=workspace,
        task_id=task.id,
        task_description=task.description or "",
        repo_name=task.repo.name if task.repo else None,
        home_dir=home_dir,
        org_id=task.organization_id,
        max_turns=40,
    )

    prompt = (
        "You are running the root-architect phase for a scaffold task.\n\n"
        "The intent grill produced this canonical statement of what the "
        "user wants:\n\n"
        "----- BEGIN INTENT -----\n"
        f"{intent_text or '(intent.md missing — improvise from the description below)'}\n"
        "----- END INTENT -----\n\n"
        f"Task title: {task.title}\n\n"
        "Now write the system-level ADR. Use the `submit-root-adr` "
        "skill to produce `.auto-agent/adrs/000-system.md`. Remember the "
        "≤10 domains cap."
    )

    target = os.path.join(workspace, ROOT_ADR_PATH)
    await agent.run(prompt, system=ROOT_ARCHITECT_SYSTEM)

    for attempt in range(1, MAX_VALIDATION_RETRIES + 1):
        adr_md = _read_text(target)
        result = validate_root_adr(adr_md)
        if result.ok:
            log.info(
                "scaffold.root_architect.valid",
                task_id=task.id,
                attempt=attempt,
            )
            return target

        log.warning(
            "scaffold.root_architect.validation_failed",
            task_id=task.id,
            attempt=attempt,
            errors=result.errors,
        )
        if attempt >= MAX_VALIDATION_RETRIES:
            break

        retry_prompt = (
            "The root ADR you wrote failed structural validation. Fix "
            "every error below and re-submit via `submit-root-adr` "
            "(overwriting the same file). Do not output the ADR in "
            "chat — just call the skill and stop.\n\n"
            "Errors:\n" + "\n".join(f"- {e}" for e in result.errors)
        )
        await agent.run(retry_prompt, system=ROOT_ARCHITECT_SYSTEM, resume=True)

    log.warning(
        "scaffold.root_architect.gave_up_on_validation",
        task_id=task.id,
        path=target,
    )
    return target


def _read_text(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""
