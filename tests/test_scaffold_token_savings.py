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


@pytest.mark.asyncio
async def test_domain_architect_prompt_does_not_inline_docs(tmp_path):
    """The domain-architect prompt must not embed intent / root ADR / grill summary bodies."""

    from agent.lifecycle.scaffold import domain_architect

    workspace = str(tmp_path)
    adrs_dir = os.path.join(workspace, ".auto-agent", "adrs")
    os.makedirs(adrs_dir, exist_ok=True)
    with open(os.path.join(workspace, ".auto-agent", "intent.md"), "w") as fh:
        fh.write("UNIQUE_INTENT_SENTINEL")
    with open(os.path.join(adrs_dir, "000-system.md"), "w") as fh:
        fh.write("# 000 — System ADR\n\n## Vision\nx\n\n## Cross-cutting concerns\n- Auth\n\n## Domains\n```yaml\ndomains:\n  - name: Tasks\n    slug: tasks\n    scope_summary: TODO items\n```\n")
    with open(os.path.join(adrs_dir, "001-tasks.grill.md"), "w") as fh:
        fh.write("UNIQUE_GRILL_SUMMARY_SENTINEL")

    task = SimpleNamespace(
        id=1,
        title="x",
        description="x",
        organization_id=7,
        repo=None,
        freeform_mode=True,
        created_by_user_id=None,
        repo_id=None,
        subtasks={},
    )

    captured: dict = {"prompts": []}

    async def fake_run(prompt, system=None, resume=False):
        captured["prompts"].append(prompt)

    fake_agent = SimpleNamespace(run=fake_run)

    async def fake_grill(_task, _domain):
        return {"status": "summary_written", "summary_path": "x"}

    with (
        patch("agent.lifecycle.scaffold.domain_architect.prepare_scaffold_workspace",
              new=AsyncMock(return_value=workspace)),
        patch("agent.lifecycle.scaffold.domain_architect.home_dir_for_task",
              new=AsyncMock(return_value=None)),
        patch("agent.lifecycle.scaffold.domain_architect.create_agent",
              return_value=fake_agent),
        patch("agent.lifecycle.scaffold.domain_grill.run", new=fake_grill),
        patch("agent.lifecycle.scaffold.domain_architect._persist_current_domain_idx",
              new=AsyncMock()),
    ):
        # Write a valid domain ADR up-front so validation does not loop.
        with open(os.path.join(adrs_dir, "001-tasks.md"), "w") as fh:
            fh.write(_VALID_DOMAIN_ADR)
        await domain_architect.run(task)

    # Only one architect prompt should have fired (1 domain), and it must not
    # contain inlined doc bodies.
    assert captured["prompts"], "domain_architect did not invoke the agent"
    arch_prompt = captured["prompts"][0]
    assert "UNIQUE_INTENT_SENTINEL" not in arch_prompt
    assert "UNIQUE_GRILL_SUMMARY_SENTINEL" not in arch_prompt
    # The root ADR text is small here; assert by the verbatim shape header.
    assert "----- BEGIN ROOT ADR -----" not in arch_prompt
    # And the prompt should point the agent at the files instead.
    assert ".auto-agent/intent.md" in arch_prompt
    assert ".auto-agent/adrs/000-system.md" in arch_prompt
    assert "001-tasks.grill.md" in arch_prompt


_VALID_DOMAIN_ADR = """\
# 001 — Tasks ADR

## Scope
This domain owns user TODO items, including their lifecycle, ownership,
and the invariants around completed vs open. It enforces that an item
belongs to exactly one owner and that completion is monotonic — once
done, an item cannot revert. The ubiquitous language is Task, Owner,
Completion, plus the lifecycle events Created, Completed, and Deleted.

## Aggregates
- Task — one user-owned todo item.

## Public surface
- Routes: GET /tasks
- Events: TaskCompleted
- Public types: Task

## Integration points
- (none)

## Affected routes
- /tasks

## Justification
Tasks are the primary domain — no other domain owns this state.
"""


@pytest.mark.asyncio
async def test_root_architect_passes_session_id_so_resume_works(tmp_path):
    """create_agent must receive a session_id; otherwise resume=True on retry is a silent no-op."""

    from agent.lifecycle.scaffold import root_architect

    workspace = str(tmp_path)
    os.makedirs(os.path.join(workspace, ".auto-agent", "adrs"), exist_ok=True)

    task = SimpleNamespace(
        id=42,
        title="x",
        description="x",
        organization_id=7,
        repo=None,
        freeform_mode=True,
        created_by_user_id=None,
    )

    captured: dict = {}

    def fake_create_agent(**kwargs):
        captured["create_agent_kwargs"] = kwargs

        async def fake_run(prompt, system=None, resume=False):
            captured.setdefault("runs", []).append({"resume": resume})

        return SimpleNamespace(run=fake_run)

    with (
        patch("agent.lifecycle.scaffold.root_architect.prepare_scaffold_workspace",
              new=AsyncMock(return_value=workspace)),
        patch("agent.lifecycle.scaffold.root_architect.home_dir_for_task",
              new=AsyncMock(return_value=None)),
        patch("agent.lifecycle.scaffold.root_architect.create_agent",
              side_effect=fake_create_agent),
    ):
        with open(os.path.join(workspace, ".auto-agent", "adrs", "000-system.md"), "w") as fh:
            fh.write(_VALID_ROOT_ADR)
        await root_architect.run(task)

    kwargs = captured["create_agent_kwargs"]
    assert kwargs.get("session_id"), "root_architect must allocate a session_id so resume works"
    # And it should be deterministic by task id so re-entry resumes the same conversation.
    assert str(task.id) in kwargs["session_id"]


@pytest.mark.asyncio
async def test_domain_architect_passes_unique_session_id_per_domain(tmp_path):
    """Each domain architect must get its own session_id so retries resume correctly."""

    from agent.lifecycle.scaffold import domain_architect

    workspace = str(tmp_path)
    adrs_dir = os.path.join(workspace, ".auto-agent", "adrs")
    os.makedirs(adrs_dir, exist_ok=True)
    with open(os.path.join(workspace, ".auto-agent", "intent.md"), "w") as fh:
        fh.write("intent")
    # Root ADR with two domains.
    with open(os.path.join(adrs_dir, "000-system.md"), "w") as fh:
        fh.write(
            "# 000 — System ADR\n\n## Vision\nx\n\n## Cross-cutting concerns\n- Auth\n\n## Domains\n"
            "```yaml\ndomains:\n  - name: A\n    slug: a\n    scope_summary: a\n"
            "  - name: B\n    slug: b\n    scope_summary: b\n```\n"
        )
    # Pre-write valid ADRs so validation does not loop.
    for idx, slug in [(1, "a"), (2, "b")]:
        with open(os.path.join(adrs_dir, f"00{idx}-{slug}.md"), "w") as fh:
            fh.write(_VALID_DOMAIN_ADR)

    task = SimpleNamespace(
        id=99,
        title="x",
        description="x",
        organization_id=7,
        repo=None,
        freeform_mode=True,
        created_by_user_id=None,
        repo_id=None,
        subtasks={},
    )

    sessions_seen: list[str] = []

    def fake_create_agent(**kwargs):
        sessions_seen.append(kwargs.get("session_id") or "")

        async def fake_run(prompt, system=None, resume=False):
            pass

        return SimpleNamespace(run=fake_run)

    async def fake_grill(_task, _domain):
        return {"status": "summary_written", "summary_path": "x"}

    with (
        patch("agent.lifecycle.scaffold.domain_architect.prepare_scaffold_workspace",
              new=AsyncMock(return_value=workspace)),
        patch("agent.lifecycle.scaffold.domain_architect.home_dir_for_task",
              new=AsyncMock(return_value=None)),
        patch("agent.lifecycle.scaffold.domain_architect.create_agent",
              side_effect=fake_create_agent),
        patch("agent.lifecycle.scaffold.domain_grill.run", new=fake_grill),
        patch("agent.lifecycle.scaffold.domain_architect._persist_current_domain_idx",
              new=AsyncMock()),
    ):
        await domain_architect.run(task)

    # Two domains -> two architect agents created, each with a non-empty,
    # distinct session_id that includes both the task id and the slug.
    assert len(sessions_seen) == 2
    assert all(sessions_seen), "every domain architect must get a session_id"
    assert len(set(sessions_seen)) == 2, "session_ids must be unique per domain"
    assert all(str(task.id) in s for s in sessions_seen)
    assert any("a" in s for s in sessions_seen) and any("b" in s for s in sessions_seen)
