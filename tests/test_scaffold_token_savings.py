"""Phase 1 — verify scaffold phase prompts no longer inline intent/root ADR bodies.

The agent will read those files via Claude Code's own tools; embedding the
full text in the prompt is pure waste in claude_cli mode.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_root_architect_prompt_does_not_inline_intent_body(tmp_path):
    """The root-architect prompt must not contain the full intent.md text."""

    from agent.lifecycle.scaffold import root_architect

    # Write a recognisable intent.md inside the scaffold workspace.
    workspace = str(tmp_path)
    intent_dir = os.path.join(workspace, ".auto-agent")
    os.makedirs(intent_dir, exist_ok=True)
    intent_body = "UNIQUE_INTENT_SENTINEL — please don't embed me literally."
    with open(os.path.join(intent_dir, "intent.md"), "w") as fh:
        fh.write(intent_body)

    task = SimpleNamespace(
        id=1,
        title="Build a TODO app",
        description="x",
        organization_id=7,
        repo=None,
        freeform_mode=True,
        created_by_user_id=None,
    )

    captured: dict = {}

    async def fake_run(prompt, system=None, resume=False):
        captured.setdefault("prompts", []).append(prompt)

    fake_agent = SimpleNamespace(run=fake_run)

    with (
        patch("agent.lifecycle.scaffold.root_architect.prepare_scaffold_workspace",
              new=AsyncMock(return_value=workspace)),
        patch("agent.lifecycle.scaffold.root_architect.home_dir_for_task",
              new=AsyncMock(return_value=None)),
        patch("agent.lifecycle.scaffold.root_architect.create_agent",
              return_value=fake_agent),
    ):
        # Also write a minimally-valid root ADR so validation doesn't loop.
        adrs_dir = os.path.join(workspace, ".auto-agent", "adrs")
        os.makedirs(adrs_dir, exist_ok=True)
        with open(os.path.join(adrs_dir, "000-system.md"), "w") as fh:
            fh.write(_VALID_ROOT_ADR)
        await root_architect.run(task)

    assert captured["prompts"], "root_architect.run must invoke the agent"
    first_prompt = captured["prompts"][0]
    assert "UNIQUE_INTENT_SENTINEL" not in first_prompt, (
        "root_architect prompt still inlines intent.md; it should instruct "
        "the agent to file_read it instead."
    )
    # And the prompt should still point the agent at the file.
    assert ".auto-agent/intent.md" in first_prompt


_VALID_ROOT_ADR = """\
# 000 — System ADR

## Vision
A small TODO app.

## Cross-cutting concerns
- Auth — TBD

## Domains
```yaml
domains:
  - name: Tasks
    slug: tasks
    scope_summary: Stores and exposes user TODO items.
```
"""


@pytest.mark.asyncio
async def test_domain_grill_prompt_does_not_inline_intent_or_root_adr(tmp_path):
    """The domain-grill prompt must not embed intent.md or root ADR bodies."""

    from agent.lifecycle.scaffold import domain_grill

    workspace = str(tmp_path)
    adrs_dir = os.path.join(workspace, ".auto-agent", "adrs")
    os.makedirs(adrs_dir, exist_ok=True)
    with open(os.path.join(workspace, ".auto-agent", "intent.md"), "w") as fh:
        fh.write("UNIQUE_INTENT_SENTINEL")
    with open(os.path.join(adrs_dir, "000-system.md"), "w") as fh:
        fh.write("UNIQUE_ROOT_ADR_SENTINEL")

    task = SimpleNamespace(
        id=1,
        title="x",
        description="x",
        organization_id=7,
        repo=None,
        freeform_mode=True,
        created_by_user_id=None,
    )
    domain = {"name": "Tasks", "slug": "tasks", "scope_summary": "TODO items", "index": 1}

    captured: dict = {}

    async def fake_run(prompt, system=None, resume=False):
        captured["prompt"] = prompt
        captured["system"] = system

    fake_agent = SimpleNamespace(run=fake_run)

    with (
        patch("agent.lifecycle.scaffold.domain_grill.prepare_scaffold_workspace",
              new=AsyncMock(return_value=workspace)),
        patch("agent.lifecycle.scaffold.domain_grill.home_dir_for_task",
              new=AsyncMock(return_value=None)),
        patch("agent.lifecycle.scaffold.domain_grill.create_agent",
              return_value=fake_agent),
    ):
        await domain_grill.run(task, domain)

    prompt = captured["prompt"]
    assert "UNIQUE_INTENT_SENTINEL" not in prompt, "intent.md still embedded"
    assert "UNIQUE_ROOT_ADR_SENTINEL" not in prompt, "root ADR still embedded"
    assert ".auto-agent/intent.md" in prompt
    assert ".auto-agent/adrs/000-system.md" in prompt
