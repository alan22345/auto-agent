"""Trio-child detection + prompt augmentation in the coding lifecycle.

A trio child is a task whose ``parent_task_id`` points at a task in
``TRIO_EXECUTING``. When the coding agent runs for one, it must:

  - load ARCHITECTURE.md from the workspace into its prompt,
  - be told about ``consult_architect`` as the escape hatch for design
    questions,
  - get the ``consult_architect`` tool registered (handled by the
    ``with_consult_architect`` flag on ``create_agent`` → exercised in
    the registry layer's own tests).

These tests cover the prompt-building seam — the smallest, DB-free unit
through which the behaviour is observable.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_coding_loads_architecture_md_for_trio_child(tmp_path):
    """_build_trio_child_prompt assembles the system prompt augmentation
    with ARCHITECTURE.md + work item description."""
    (tmp_path / "ARCHITECTURE.md").write_text("# This app\n\nUse Postgres.")

    from agent.lifecycle.coding import _build_trio_child_prompt

    work_item_description = "Add auth"
    prompt = await _build_trio_child_prompt(
        child_description=work_item_description,
        workspace=str(tmp_path),
    )

    assert "ARCHITECTURE.md" in prompt
    assert "Use Postgres." in prompt
    assert "Add auth" in prompt
    assert "consult_architect" in prompt.lower()


@pytest.mark.asyncio
async def test_build_trio_child_prompt_handles_missing_architecture_md(tmp_path):
    from agent.lifecycle.coding import _build_trio_child_prompt

    prompt = await _build_trio_child_prompt(
        child_description="x",
        workspace=str(tmp_path),
    )
    assert "ARCHITECTURE.md" in prompt
    assert "not found" in prompt.lower() or "missing" in prompt.lower()
