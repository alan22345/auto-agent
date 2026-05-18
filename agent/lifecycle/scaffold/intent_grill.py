"""Phase A — intent grill — ADR-018 §2.

Runs one agent against the task description with the intent-grill
system prompt. The agent calls the ``submit-intent-summary`` skill,
which writes ``.auto-agent/intent.md``. The orchestrator reads that
file back here to confirm and to surface it on UI.

When ``task.freeform_mode is True``, the PO standin
(``agent.po_agent.po_answer_intent_grill``) answers any pending grill
question instead of escaping to the user — see ADR-018 §2 + ADR-015 §6.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.scaffold._workspace import prepare_scaffold_workspace
from agent.lifecycle.scaffold.prompts import INTENT_GRILL_SYSTEM
from agent.lifecycle.workspace_paths import INTENT_PATH

if TYPE_CHECKING:
    from shared.models import Task

log = structlog.get_logger()


async def request_po_intent_answer(task: Task, question: str) -> str:
    """Ask the PO standin to answer a pending intent-grill question.

    Thin wrapper around ``agent.po_agent.po_answer_intent_grill`` so the
    scaffold modules don't have to know about ``po_agent`` directly.
    Returns the absolute path the PO wrote to.

    Caller is responsible for checking ``task.freeform_mode`` before
    invoking this — in human-in-loop mode the user answers via the UI.
    """

    from agent.po_agent import po_answer_intent_grill

    workspace = await prepare_scaffold_workspace(task)
    await po_answer_intent_grill(task, question, workspace)
    from agent.lifecycle.workspace_paths import INTENT_GRILL_ANSWER_PATH

    return os.path.join(workspace, INTENT_GRILL_ANSWER_PATH)


async def run(task: Task) -> str:
    """Run the intent-grill agent. Returns the path to ``intent.md``.

    The agent writes the file via the ``submit-intent-summary`` skill;
    we don't construct the markdown here. On a degraded run (skill not
    invoked) we leave the file missing and let the caller decide whether
    to retry. The v1 contract: log a warning, advance anyway — the next
    phase will fail-fast when it can't read intent.md.
    """

    workspace = await prepare_scaffold_workspace(task)
    home_dir = await home_dir_for_task(task)

    agent = create_agent(
        workspace=workspace,
        task_id=task.id,
        task_description=task.description or "",
        repo_name=task.repo.name if task.repo else None,
        home_dir=home_dir,
        org_id=task.organization_id,
        max_turns=40,
    )

    freeform = bool(getattr(task, "freeform_mode", False))
    standin_hint = (
        (
            "\n\nFREEFORM MODE: there is no human in the loop. When you would "
            "normally pause to ask the user a question, instead infer the most "
            "reasonable answer from the task description and proceed; the PO "
            "standin will not be polled mid-turn. Aim to write the intent doc "
            "in one pass."
        )
        if freeform
        else ""
    )

    prompt = (
        "You are running the intent-grill phase for a brand-new scaffold "
        "task.\n\n"
        f"Task title: {task.title}\n\n"
        f"Task description:\n{task.description or '(empty)'}\n\n"
        "Grill the user (or the freeform PO standin if no human is in the "
        "loop) until you can write the intent doc. Then call the "
        "`submit-intent-summary` skill and stop." + standin_hint
    )

    await agent.run(prompt, system=INTENT_GRILL_SYSTEM)

    target = os.path.join(workspace, INTENT_PATH)
    if not os.path.isfile(target):
        log.warning(
            "scaffold.intent_grill.intent_md_missing",
            task_id=task.id,
            path=target,
        )
    else:
        log.info(
            "scaffold.intent_grill.complete",
            task_id=task.id,
            path=target,
        )
    return target
