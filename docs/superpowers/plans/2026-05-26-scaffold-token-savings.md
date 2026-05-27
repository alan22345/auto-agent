# Scaffold Token Savings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut scaffold-run token consumption by removing redundant prompt content, restoring broken `--resume` behavior, caching deterministic results, and adding the missing telemetry needed to verify savings.

**Architecture:** The auto-agent uses `LLM_PROVIDER=claude_cli` by default. Each scaffold phase calls `create_agent(...).run(prompt)`, which fires a fresh `claude --print` subprocess. The fixed overhead per session (~25-30K tokens of Claude Code system prompt + project `CLAUDE.md` + 27 skill descriptions + tool defs) is paid once *per phase*. We attack the four highest-leverage waste sources: (1) inline-embedded docs the agent will also `file_read`, (2) a silently-no-op'd `resume=True` path in scaffold architects, (3) un-cached vision-LLM calls in `inspect_ui` across final-verify rounds, and (4) the missing usage telemetry that prevents measurement.

**Tech Stack:** Python 3.12, asyncio, pytest, structlog, SQLAlchemy async, Pydantic. The CLI provider lives at `agent/llm/claude_cli.py` and shells out to `claude --print`.

**Estimated savings (7-domain scaffold parent only):** ~125K input tokens from Phase 1, ~50K per architect retry from Phase 2, ~10-30K per re-verify round from Phase 3. Phase 0 makes the savings measurable.

---

## Pre-flight

Before Task 1, confirm:

- [ ] Current working directory: `/Users/alanyeginchibayev/Documents/Github/auto-agent`
- [ ] Baseline test suite is green: run `.venv/bin/python3 -m pytest tests/ -q` and confirm PASS (or note any pre-existing failures so they're not blamed on this work).
- [ ] Linter is clean: `ruff check .` returns no errors.

---

## Phase 0 — Restore token telemetry for `claude_cli`

**Why first:** `agent/llm/claude_cli.py:83` returns `TokenUsage()` (zeros) for every CLI call. Every later phase needs real numbers to prove it worked. `claude --print` supports `--output-format=json`, whose final JSON envelope includes a `usage` object with `input_tokens` / `output_tokens` (and cache counters). We parse that envelope.

### Task 1: Wire `--output-format=json` and surface usage

**Files:**
- Modify: `agent/llm/claude_cli.py`
- Test: `tests/test_claude_cli_usage.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_cli_usage.py`:

```python
"""Verify ClaudeCLIProvider parses --output-format=json envelope and reports usage."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.llm.claude_cli import ClaudeCLIProvider
from agent.llm.types import Message


@pytest.mark.asyncio
async def test_complete_parses_usage_from_json_envelope():
    """The provider must extract input/output token counts from the result envelope."""

    envelope = {
        "type": "result",
        "subtype": "success",
        "result": "Hello from Claude.",
        "session_id": "sid-1",
        "total_cost_usd": 0.0123,
        "duration_ms": 4567,
        "usage": {
            "input_tokens": 12345,
            "output_tokens": 678,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    fake_stdout = (json.dumps(envelope) + "\n").encode()

    provider = ClaudeCLIProvider()

    async def fake_invoke(prompt: str):
        return (fake_stdout.decode(), "", 0)

    with patch.object(provider, "_invoke_cli_once", new=AsyncMock(side_effect=fake_invoke)):
        response = await provider.complete(
            messages=[Message(role="user", content="hi")],
        )

    assert response.message.content == "Hello from Claude."
    assert response.usage.input_tokens == 12345
    assert response.usage.output_tokens == 678


@pytest.mark.asyncio
async def test_complete_tolerates_non_json_stdout():
    """If parsing fails (older Claude Code), fall back to plain text + zero usage."""

    provider = ClaudeCLIProvider()

    async def fake_invoke(prompt: str):
        return ("plain text not JSON", "", 0)

    with patch.object(provider, "_invoke_cli_once", new=AsyncMock(side_effect=fake_invoke)):
        response = await provider.complete(
            messages=[Message(role="user", content="hi")],
        )

    # Plain text is returned verbatim; usage stays zero (best-effort).
    assert response.message.content == "plain text not JSON"
    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 0


@pytest.mark.asyncio
async def test_invoke_cli_passes_output_format_json_flag():
    """The CLI invocation must include --output-format=json so usage is reported."""

    captured_cmd: list[str] = []

    async def fake_create_subprocess(*cmd, **kwargs):
        captured_cmd.extend(cmd)

        class _P:
            returncode = 0

            async def communicate(self):
                return (b'{"type":"result","result":"ok","usage":{"input_tokens":1,"output_tokens":1}}', b"")

        return _P()

    provider = ClaudeCLIProvider()
    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess):
        await provider._invoke_cli_once("hello")

    assert "--output-format" in captured_cmd
    idx = captured_cmd.index("--output-format")
    assert captured_cmd[idx + 1] == "json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_claude_cli_usage.py -v`
Expected: 3 tests FAIL — first two because usage is hardcoded to zeros, third because `--output-format=json` is not yet in the command.

- [ ] **Step 3: Implement the changes**

Edit `agent/llm/claude_cli.py`:

(a) Add `--output-format`, `json` to the `cmd` list in `_invoke_cli_once`. Find the current line:

```python
cmd = ["claude", "--print", "--dangerously-skip-permissions"]
```

Replace with:

```python
cmd = ["claude", "--print", "--output-format", "json", "--dangerously-skip-permissions"]
```

(b) Replace the `complete` method body. Current:

```python
output = await self._run_cli(prompt)
return LLMResponse(
    message=Message(role="assistant", content=output),
    stop_reason="end_turn",
    usage=TokenUsage(),  # CLI doesn't report token usage
)
```

Replace with:

```python
import json as _json

raw = await self._run_cli(prompt)
text = raw
usage = TokenUsage()
try:
    envelope = _json.loads(raw)
    if isinstance(envelope, dict):
        # Final-result envelope shape:
        # {"type":"result","result":"...","usage":{"input_tokens":N,"output_tokens":M, ...}}
        result_text = envelope.get("result")
        if isinstance(result_text, str):
            text = result_text
        u = envelope.get("usage")
        if isinstance(u, dict):
            in_tok = int(u.get("input_tokens") or 0)
            out_tok = int(u.get("output_tokens") or 0)
            # Cache reads are billed at a fraction; we surface them as input
            # tokens for now (UsageSink doesn't distinguish yet).
            cache_read = int(u.get("cache_read_input_tokens") or 0)
            usage = TokenUsage(
                input_tokens=in_tok + cache_read,
                output_tokens=out_tok,
            )
except (ValueError, TypeError):
    # Non-JSON output (older Claude Code, or an error path). Surface the
    # raw text and leave usage at zero — emit_usage_event will record a
    # zero-token event rather than crash.
    pass

return LLMResponse(
    message=Message(role="assistant", content=text),
    stop_reason="end_turn",
    usage=usage,
)
```

Also add a top-of-file import (next to the existing `import json` if present, else add a new line below existing imports):

```python
# `json` is already imported lazily inside complete(); no top-level import needed.
```

(If the file does not already import `json` at the top, the lazy `import json as _json` inside `complete` is sufficient — keep it.)

- [ ] **Step 4: Run the new tests**

Run: `.venv/bin/python3 -m pytest tests/test_claude_cli_usage.py -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Run existing claude_cli tests to confirm no regressions**

Run: `.venv/bin/python3 -m pytest tests/test_claude_cli_home_dir.py tests/test_claude_cli_session_recovery.py -v`
Expected: All tests PASS (these mock at the subprocess level and should be unaffected; if any fail because they now need to return JSON, update the mocks to return a minimal `{"type":"result","result":"<output>","usage":{"input_tokens":0,"output_tokens":0}}` envelope).

- [ ] **Step 6: Run the full unit suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: All PASS.

- [ ] **Step 7: Lint**

Run: `ruff check agent/llm/claude_cli.py tests/test_claude_cli_usage.py`
Expected: Clean.

- [ ] **Step 8: Commit**

```bash
git add agent/llm/claude_cli.py tests/test_claude_cli_usage.py
git commit -m "feat(claude_cli): parse --output-format=json envelope to populate TokenUsage

The claude_cli provider was returning TokenUsage() with zeros, so every
usage_events row for prod scaffolds had input_tokens=output_tokens=0.
Switching to --output-format=json gives us the final-result envelope
with a usage object (input_tokens, output_tokens, cache_*). Parse it,
fall back to raw text + zero usage if the output is non-JSON.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 1 — Stop embedding scaffold docs into prompts; delete dead constant

**Why:** Three scaffold phase prompts (`root_architect`, `domain_grill`, `domain_architect`) inline `.auto-agent/intent.md` and `.auto-agent/adrs/000-system.md` as `----- BEGIN -----` blocks. Claude Code then *also* `file_read`s them. That's 100% redundant. We instruct the agent to read the files from disk and drop the inline blocks. For a 7-domain scaffold this saves ~125K input tokens. The same file also defines `FINAL_VERIFICATION_SYSTEM` which is never imported anywhere — delete it.

### Task 2: Slim the `root_architect` prompt

**Files:**
- Modify: `agent/lifecycle/scaffold/root_architect.py:48-83`
- Test: `tests/test_scaffold_token_savings.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_scaffold_token_savings.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py::test_root_architect_prompt_does_not_inline_intent_body -v`
Expected: FAIL with "root_architect prompt still inlines intent.md".

- [ ] **Step 3: Slim the prompt**

In `agent/lifecycle/scaffold/root_architect.py`, replace the `run` function body's prompt-construction block. Find:

```python
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
```

Replace with:

```python
    intent_path = os.path.join(workspace, INTENT_PATH)
    intent_present = os.path.isfile(intent_path)

    agent = create_agent(
```

Then find:

```python
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
```

Replace with:

```python
    if intent_present:
        intent_hint = (
            f"Read `{INTENT_PATH}` from the workspace — it is the canonical "
            "statement of what the user wants. Do not skip it."
        )
    else:
        intent_hint = (
            f"`{INTENT_PATH}` is missing — improvise from the task title and "
            "description below."
        )

    prompt = (
        "You are running the root-architect phase for a scaffold task.\n\n"
        f"{intent_hint}\n\n"
        f"Task title: {task.title}\n\n"
        "Now write the system-level ADR. Use the `submit-root-adr` "
        "skill to produce `.auto-agent/adrs/000-system.md`. Remember the "
        "≤10 domains cap."
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py::test_root_architect_prompt_does_not_inline_intent_body -v`
Expected: PASS.

- [ ] **Step 5: Run all scaffold tests**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_lifecycle.py tests/test_scaffold_e2e.py tests/test_scaffold_state_machine.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/scaffold/root_architect.py tests/test_scaffold_token_savings.py
git commit -m "perf(scaffold): root_architect reads intent.md instead of inlining it

The root_architect prompt was embedding the entire intent.md body. Claude
Code's own file_read tool reaches it anyway. For a typical 1-3 KB intent,
this is wasted on each session start. Replace the inline block with a
'read this file' hint and pass only a presence-or-missing signal.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 3: Slim the `domain_grill` prompt

**Files:**
- Modify: `agent/lifecycle/scaffold/domain_grill.py:160-242`
- Test: extend `tests/test_scaffold_token_savings.py`

- [ ] **Step 1: Extend the test file**

Append to `tests/test_scaffold_token_savings.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py::test_domain_grill_prompt_does_not_inline_intent_or_root_adr -v`
Expected: FAIL.

- [ ] **Step 3: Slim the prompt**

In `agent/lifecycle/scaffold/domain_grill.py`, replace the file-reading + prompt-construction block. Find:

```python
    intent_text = ""
    intent_path = os.path.join(workspace, INTENT_PATH)
    if os.path.isfile(intent_path):
        try:
            with open(intent_path) as fh:
                intent_text = fh.read()
        except OSError:
            pass

    root_adr_text = ""
    root_adr_abs = os.path.join(workspace, ROOT_ADR_PATH)
    if os.path.isfile(root_adr_abs):
        try:
            with open(root_adr_abs) as fh:
                root_adr_text = fh.read()
        except OSError:
            pass
```

Replace with:

```python
    intent_present = os.path.isfile(os.path.join(workspace, INTENT_PATH))
    root_adr_present = os.path.isfile(os.path.join(workspace, ROOT_ADR_PATH))
```

Then find the `prompt = (` block:

```python
    prompt = (
        f"You are running the per-domain grill for **{name}** "
        f"(slug `{slug}`, index {index}).\n\n"
        f"Your domain's scope summary from the root ADR:\n> {scope_summary}\n\n"
        "----- BEGIN INTENT -----\n"
        f"{intent_text or '(intent.md missing)'}\n"
        "----- END INTENT -----\n\n"
        "----- BEGIN ROOT ADR -----\n"
        f"{root_adr_text or '(000-system.md missing)'}\n"
        "----- END ROOT ADR -----"
        + prior_answer_block
        + standin_hint
        + "\n\nGrill the user (or freeform standin) until you can write a "
        f"complete grill summary at `{summary_rel}`. When you need an answer "
        "from the user, call `submit-domain-grill-question` and stop. When "
        "you're done, call `submit-domain-grill-summary` and stop."
    )
```

Replace with:

```python
    intent_line = (
        f"Read `{INTENT_PATH}` — it states what the user wants for the overall product."
        if intent_present else f"`{INTENT_PATH}` is missing."
    )
    root_adr_line = (
        f"Read `{ROOT_ADR_PATH}` — it places this domain in the wider system."
        if root_adr_present else f"`{ROOT_ADR_PATH}` is missing."
    )

    prompt = (
        f"You are running the per-domain grill for **{name}** "
        f"(slug `{slug}`, index {index}).\n\n"
        f"Your domain's scope summary from the root ADR:\n> {scope_summary}\n\n"
        f"{intent_line}\n"
        f"{root_adr_line}\n"
        + prior_answer_block
        + standin_hint
        + "\n\nGrill the user (or freeform standin) until you can write a "
        f"complete grill summary at `{summary_rel}`. When you need an answer "
        "from the user, call `submit-domain-grill-question` and stop. When "
        "you're done, call `submit-domain-grill-summary` and stop."
    )
```

- [ ] **Step 4: Verify the test passes**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py -v`
Expected: All previous tests still PASS, new test PASS.

- [ ] **Step 5: Run scaffold integration tests**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_lifecycle.py tests/test_scaffold_e2e.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/scaffold/domain_grill.py tests/test_scaffold_token_savings.py
git commit -m "perf(scaffold): domain_grill reads intent + root ADR instead of inlining

domain_grill embedded intent.md (~2K tokens) + root ADR (~5K tokens) into
the agent prompt for each of N domains. For a 7-domain scaffold this was
~50K wasted input tokens — Claude Code reads those files via its own
tools anyway. Replace the inline blocks with file-path hints.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 4: Slim the `domain_architect` prompt

**Files:**
- Modify: `agent/lifecycle/scaffold/domain_architect.py:128-289`
- Test: extend `tests/test_scaffold_token_savings.py`

- [ ] **Step 1: Extend the test file**

Append to `tests/test_scaffold_token_savings.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py::test_domain_architect_prompt_does_not_inline_docs -v`
Expected: FAIL.

- [ ] **Step 3: Slim the prompt**

In `agent/lifecycle/scaffold/domain_architect.py`, inside the `run` function find the prompt-construction block:

```python
        grill_summary_rel = domain_grill_path(idx, slug)
        grill_summary_abs = os.path.join(workspace, grill_summary_rel)
        grill_summary_text = _read_text(grill_summary_abs)
```

Replace with:

```python
        grill_summary_rel = domain_grill_path(idx, slug)
        grill_summary_abs = os.path.join(workspace, grill_summary_rel)
        grill_summary_present = os.path.isfile(grill_summary_abs)
```

Then find:

```python
        prompt = (
            f"You are the domain architect for **{name}** "
            f"(slug `{slug}`, index {idx}).\n\n"
            "The root ADR placed this domain in the system as:\n\n"
            "----- BEGIN ROOT ADR -----\n"
            f"{root_adr_md}\n"
            "----- END ROOT ADR -----\n\n"
            "----- BEGIN INTENT -----\n"
            f"{intent_text or '(intent.md missing)'}\n"
            "----- END INTENT -----\n\n"
            "----- BEGIN DOMAIN GRILL SUMMARY (authoritative for this domain) -----\n"
            f"{grill_summary_text or '(grill summary missing — fall back to root scope_summary)'}\n"
            "----- END DOMAIN GRILL SUMMARY -----\n\n"
            f"Your domain's scope summary from the root ADR:\n"
            f"> {domain.get('scope_summary') or ''}\n\n"
            "Treat the grill summary as the user's voice. Where it conflicts "
            "with your instinct, follow the grill summary. Write the domain "
            f"ADR via the `submit-domain-adr` skill. Target path: `{target_rel}`."
        )
```

Replace with:

```python
        grill_hint = (
            f"Read `{grill_summary_rel}` — it is authoritative for this domain "
            "and is the user's voice. Where it conflicts with your instinct, "
            "follow the grill summary."
            if grill_summary_present
            else f"`{grill_summary_rel}` is missing — fall back to the scope summary below."
        )

        prompt = (
            f"You are the domain architect for **{name}** "
            f"(slug `{slug}`, index {idx}).\n\n"
            f"Read `{ROOT_ADR_PATH}` for the system decomposition and "
            f"`{INTENT_PATH}` for the original user intent.\n\n"
            f"{grill_hint}\n\n"
            f"Your domain's scope summary from the root ADR:\n"
            f"> {domain.get('scope_summary') or ''}\n\n"
            "Write the domain ADR via the `submit-domain-adr` skill. "
            f"Target path: `{target_rel}`."
        )
```

You can now also remove the unused `intent_text` variable (defined earlier in `run`). Find:

```python
    intent_text = _read_text(os.path.join(workspace, INTENT_PATH))
```

Delete that line.

You can also remove the `root_adr_md` read if it is no longer used elsewhere in the function — but `parse_domains(root_adr_md)` near the top still uses it, so keep that. Search for other `root_adr_md` uses *inside* the `for loop_idx, domain in enumerate(domains)` loop: only the prompt referenced it; that reference is now gone, so the var is unused inside the loop only — it is still read once outside the loop. No further changes.

- [ ] **Step 4: Verify the test passes**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Run scaffold integration tests**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_lifecycle.py tests/test_scaffold_e2e.py tests/test_scaffold_state_machine.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/scaffold/domain_architect.py tests/test_scaffold_token_savings.py
git commit -m "perf(scaffold): domain_architect reads docs instead of inlining

The per-domain architect prompt was inlining intent.md + root ADR +
grill summary in full. For 7 domains this is ~63K wasted input tokens.
Replace with file-path hints; Claude Code's file_read tool reaches them
on demand.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 5: Delete dead `FINAL_VERIFICATION_SYSTEM` constant

**Files:**
- Modify: `agent/lifecycle/scaffold/prompts.py:302-333`

- [ ] **Step 1: Confirm no imports remain**

Run: `grep -rn "FINAL_VERIFICATION_SYSTEM" --include="*.py" /Users/alanyeginchibayev/Documents/Github/auto-agent | grep -v worktrees | grep -v __pycache__`
Expected output: only one line — the definition in `agent/lifecycle/scaffold/prompts.py`. If anything else appears, STOP and investigate before deleting.

- [ ] **Step 2: Delete the constant**

In `agent/lifecycle/scaffold/prompts.py`, delete lines 302-333 (the entire `FINAL_VERIFICATION_SYSTEM = """\` block and its closing `"""`).

- [ ] **Step 3: Verify Python parses the file**

Run: `.venv/bin/python3 -c "from agent.lifecycle.scaffold import prompts; print(dir(prompts))"`
Expected: clean output, `FINAL_VERIFICATION_SYSTEM` not in the listing.

- [ ] **Step 4: Run scaffold tests**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_lifecycle.py tests/test_scaffold_e2e.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/lifecycle/scaffold/prompts.py
git commit -m "chore(scaffold): drop unused FINAL_VERIFICATION_SYSTEM prompt

final_verification.py is deterministic — it composes a verdict from
verify_primitives results without ever calling the LLM. The prompt was
imported nowhere outside its own module, so it was loaded into memory
on every import for no benefit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — Fix the broken `resume=True` path in scaffold architects

**Why:** `root_architect.py:115` and `domain_architect.py:313, 341` pass `resume=True` on validation-retry runs. But `create_agent(...)` was called with no `session_id`, so `factory.py:113` sets `session=None`, so `loop.py:240` skips `set_session`, so the underlying `claude --print` runs with no `--resume` and no `--session-id`. Every retry pays the full ~25-30K reload cost. We allocate a deterministic session_id per phase so retries are real `--resume` calls.

### Task 6: Allocate session_id in `root_architect` so resume actually resumes

**Files:**
- Modify: `agent/lifecycle/scaffold/root_architect.py:62-115`
- Test: extend `tests/test_scaffold_token_savings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scaffold_token_savings.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py::test_root_architect_passes_session_id_so_resume_works -v`
Expected: FAIL with "must allocate a session_id".

- [ ] **Step 3: Pass session_id in root_architect.create_agent**

In `agent/lifecycle/scaffold/root_architect.py`, find:

```python
    agent = create_agent(
        workspace=workspace,
        task_id=task.id,
        task_description=task.description or "",
        repo_name=task.repo.name if task.repo else None,
        home_dir=home_dir,
        org_id=task.organization_id,
        max_turns=40,
    )
```

Replace with:

```python
    # Allocate a deterministic session_id so the validator-retry runs below
    # can actually --resume the prior CLI session instead of paying the full
    # system-prompt reload cost (see plans/2026-05-26-scaffold-token-savings.md
    # Phase 2). The session_id is per-phase per-task; on retry we pass
    # resume=True and AgentLoop._run_passthrough wires it through.
    session_id = f"scaffold-{task.id}-root-architect"
    agent = create_agent(
        workspace=workspace,
        session_id=session_id,
        task_id=task.id,
        task_description=task.description or "",
        repo_name=task.repo.name if task.repo else None,
        home_dir=home_dir,
        org_id=task.organization_id,
        max_turns=40,
    )
```

- [ ] **Step 4: Verify the test passes**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py::test_root_architect_passes_session_id_so_resume_works -v`
Expected: PASS.

- [ ] **Step 5: Confirm full token-savings test suite still passes**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py tests/test_scaffold_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/scaffold/root_architect.py tests/test_scaffold_token_savings.py
git commit -m "fix(scaffold): allocate session_id in root_architect so resume=True works

root_architect.run() passed resume=True on validator retries, but
create_agent was called without a session_id, so Session was None,
so claude_cli.set_session was never called — the CLI ran fresh on
every retry. Allocate a deterministic session_id (\"scaffold-<task>-root-architect\")
so the retry's --resume actually picks up the prior session.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 7: Allocate session_id per-domain in `domain_architect`

**Files:**
- Modify: `agent/lifecycle/scaffold/domain_architect.py:279-341`
- Test: extend `tests/test_scaffold_token_savings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scaffold_token_savings.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py::test_domain_architect_passes_unique_session_id_per_domain -v`
Expected: FAIL.

- [ ] **Step 3: Allocate a per-domain session_id**

In `agent/lifecycle/scaffold/domain_architect.py`, find:

```python
        agent = create_agent(
            workspace=workspace,
            task_id=task.id,
            task_description=task.description or "",
            repo_name=task.repo.name if task.repo else None,
            home_dir=home_dir,
            org_id=task.organization_id,
            max_turns=40,
        )
```

Replace with:

```python
        # Allocate a per-domain session_id so validation retries below
        # (and the secrets-manifest retry) actually --resume the prior CLI
        # session. See plans/2026-05-26-scaffold-token-savings.md Phase 2.
        session_id = f"scaffold-{task.id}-domain-architect-{slug}"
        agent = create_agent(
            workspace=workspace,
            session_id=session_id,
            task_id=task.id,
            task_description=task.description or "",
            repo_name=task.repo.name if task.repo else None,
            home_dir=home_dir,
            org_id=task.organization_id,
            max_turns=40,
        )
```

- [ ] **Step 4: Verify the test passes**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_token_savings.py::test_domain_architect_passes_unique_session_id_per_domain -v`
Expected: PASS.

- [ ] **Step 5: Run all scaffold tests**

Run: `.venv/bin/python3 -m pytest tests/test_scaffold_lifecycle.py tests/test_scaffold_e2e.py tests/test_scaffold_state_machine.py tests/test_scaffold_token_savings.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/scaffold/domain_architect.py tests/test_scaffold_token_savings.py
git commit -m "fix(scaffold): allocate per-domain session_id in domain_architect

Same broken-resume issue as root_architect: domain_architect passed
resume=True on validation + secrets-manifest retries, but the agent
had no session_id so Session was None and the --resume flag was
silently dropped. Allocate session_id per domain so each domain's
retries are real resumes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — Cache `inspect_ui` vision-LLM results across final-verify rounds

**Why:** `agent/lifecycle/scaffold/final_verification.py:175` calls `verify_primitives.inspect_ui` once per UI route. The scaffold parent can loop final-verify up to 3 times (`MAX_FINAL_VERIFY_ROUNDS = 3` in `parent.py:38`). Without caching, every route is judged on every round even if the UI did not change. We add a content-keyed cache so a screenshot+intent pair that has been judged before short-circuits.

### Task 8: Cache `inspect_ui` by (route, intent_hash, screenshot_hash)

**Files:**
- Read first to understand current shape: `agent/lifecycle/verify_primitives.py` (whole file)
- Modify: `agent/lifecycle/verify_primitives.py` (the `inspect_ui` function and add a cache helper)
- Test: `tests/test_inspect_ui_cache.py` (NEW)

- [ ] **Step 1: Read `verify_primitives.py` and find the `inspect_ui` function**

Use `Read` to load `/Users/alanyeginchibayev/Documents/Github/auto-agent/agent/lifecycle/verify_primitives.py` in full. Locate `async def inspect_ui(...)` and note: (a) what params it takes, (b) where it captures the screenshot, (c) where it calls the vision LLM. The cache key must include the screenshot bytes' SHA-256 + a hash of the `intent` string + the `route`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_inspect_ui_cache.py`:

```python
"""Phase 3 — verify inspect_ui short-circuits on repeated identical inputs."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle import verify_primitives


@pytest.mark.asyncio
async def test_inspect_ui_uses_cached_result_when_inputs_unchanged(tmp_path, monkeypatch):
    """A second call with the same (route, intent, screenshot bytes) must not invoke the vision LLM."""

    # Reset module-level cache for hermetic test.
    verify_primitives._INSPECT_UI_CACHE.clear()  # noqa: SLF001

    screenshot_bytes = b"fake-png-content-stable"
    vision_calls = {"n": 0}

    async def fake_screenshot(*_a, **_kw):
        return screenshot_bytes

    async def fake_vision_judge(*_a, **_kw):
        vision_calls["n"] += 1

        class _R:
            ok = True
            reason = ""

        return _R()

    with (
        patch.object(verify_primitives, "_capture_route_screenshot",
                     new=AsyncMock(side_effect=fake_screenshot)),
        patch.object(verify_primitives, "_vision_judge_screenshot",
                     new=AsyncMock(side_effect=fake_vision_judge)),
    ):
        r1 = await verify_primitives.inspect_ui(route="/dash", intent="show dashboard", base_url="http://x")
        r2 = await verify_primitives.inspect_ui(route="/dash", intent="show dashboard", base_url="http://x")

    assert r1.ok and r2.ok
    assert vision_calls["n"] == 1, (
        "vision judge was called twice for identical inputs — cache miss"
    )


@pytest.mark.asyncio
async def test_inspect_ui_does_not_cache_across_different_intent(tmp_path):
    """Different intent strings must produce two vision-judge calls."""

    verify_primitives._INSPECT_UI_CACHE.clear()  # noqa: SLF001

    vision_calls = {"n": 0}

    async def fake_screenshot(*_a, **_kw):
        return b"fake-png"

    async def fake_vision_judge(*_a, **_kw):
        vision_calls["n"] += 1

        class _R:
            ok = True
            reason = ""

        return _R()

    with (
        patch.object(verify_primitives, "_capture_route_screenshot",
                     new=AsyncMock(side_effect=fake_screenshot)),
        patch.object(verify_primitives, "_vision_judge_screenshot",
                     new=AsyncMock(side_effect=fake_vision_judge)),
    ):
        await verify_primitives.inspect_ui(route="/x", intent="A", base_url="http://x")
        await verify_primitives.inspect_ui(route="/x", intent="B", base_url="http://x")

    assert vision_calls["n"] == 2
```

NOTE: the test refers to `_capture_route_screenshot` and `_vision_judge_screenshot` as the seams to patch. **If the current `inspect_ui` does not yet have those helpers exposed**, your first step in the implementation is to extract them so the test can patch them cleanly — that refactor is part of this task.

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_inspect_ui_cache.py -v`
Expected: FAIL — either because the cache attr doesn't exist, the helpers don't exist, or the cache doesn't short-circuit.

- [ ] **Step 4: Implement the cache**

In `agent/lifecycle/verify_primitives.py`:

(a) Near the top of the module (after imports), add the module-level cache + helpers:

```python
import hashlib as _hashlib

# Module-level cache for inspect_ui verdicts.
# Key: SHA-256 of f"{route}\x00{intent}\x00<screenshot-hex>".
# Value: an InspectUIResult-like object (the same shape inspect_ui returns).
# Bounded informally — final-verify caps at 3 rounds × O(routes) entries per
# scaffold parent, so an unbounded dict is acceptable for v1. If this ever
# grows, switch to functools.lru_cache or an LRU dict.
_INSPECT_UI_CACHE: dict[str, object] = {}


def _inspect_ui_cache_key(route: str, intent: str, screenshot: bytes) -> str:
    h = _hashlib.sha256()
    h.update(route.encode("utf-8"))
    h.update(b"\x00")
    h.update(intent.encode("utf-8"))
    h.update(b"\x00")
    h.update(screenshot)
    return h.hexdigest()
```

(b) Refactor `inspect_ui` so the screenshot capture and the vision-judge call are extracted into private helpers (so the test can patch them). The exact diff depends on the current shape — keep the public signature and return type. Surround the vision-judge call with the cache check:

```python
async def inspect_ui(*, route: str, intent: str, base_url: str):
    screenshot = await _capture_route_screenshot(route=route, base_url=base_url)
    if screenshot is None:
        # Playwright unavailable / failed — fall through with existing behavior.
        return await _vision_judge_screenshot(route=route, intent=intent, screenshot=None)

    cache_key = _inspect_ui_cache_key(route, intent, screenshot)
    cached = _INSPECT_UI_CACHE.get(cache_key)
    if cached is not None:
        return cached

    result = await _vision_judge_screenshot(route=route, intent=intent, screenshot=screenshot)
    _INSPECT_UI_CACHE[cache_key] = result
    return result
```

The two helpers `_capture_route_screenshot` and `_vision_judge_screenshot` carry the body that `inspect_ui` previously did inline. Preserve all existing error handling, especially the `playwright_not_installed` branch (the test patches both helpers, so the production code paths are unaffected as long as the seams exist).

- [ ] **Step 5: Verify the new tests pass**

Run: `.venv/bin/python3 -m pytest tests/test_inspect_ui_cache.py -v`
Expected: 2 tests PASS.

- [ ] **Step 6: Run any existing verify_primitives tests**

Run: `.venv/bin/python3 -m pytest tests/test_verify_primitives_env.py -v`
Expected: PASS. If a test breaks because it directly called the previous `inspect_ui` internals, update the mock to patch the new helper.

- [ ] **Step 7: Run full unit suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 8: Lint**

Run: `ruff check agent/lifecycle/verify_primitives.py tests/test_inspect_ui_cache.py`
Expected: Clean.

- [ ] **Step 9: Commit**

```bash
git add agent/lifecycle/verify_primitives.py tests/test_inspect_ui_cache.py
git commit -m "perf(verify): cache inspect_ui results by (route, intent, screenshot) hash

The scaffold parent runs final_verification up to MAX_FINAL_VERIFY_ROUNDS=3
times. inspect_ui makes a vision-LLM call per UI route on every round
even when the UI hasn't changed between rounds. Hash the (route, intent,
screenshot bytes) tuple and short-circuit on repeat. For a typical
scaffold with N UI routes and R verify rounds this saves up to (R-1)*N
vision calls.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 4 — Wrap-up + measurement

### Task 9: Final verification

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: All PASS, no regressions.

- [ ] **Step 2: Lint everything touched**

Run: `ruff check agent/llm/claude_cli.py agent/lifecycle/scaffold/ agent/lifecycle/verify_primitives.py tests/test_claude_cli_usage.py tests/test_scaffold_token_savings.py tests/test_inspect_ui_cache.py`
Expected: Clean.

- [ ] **Step 3: Format check**

Run: `ruff format --check agent/llm/claude_cli.py agent/lifecycle/scaffold/ agent/lifecycle/verify_primitives.py tests/test_claude_cli_usage.py tests/test_scaffold_token_savings.py tests/test_inspect_ui_cache.py`
Expected: Clean. If anything reformats, run `ruff format` on the same paths and commit the formatting fix.

- [ ] **Step 4: Review the diff**

Run: `git log --oneline main..HEAD`
Expected: 8 focused commits, one per task.

Run: `git diff --stat main..HEAD`
Sanity-check the diff stats — each commit should touch the file(s) named in its task, plus its test.

- [ ] **Step 5: Note the open follow-ups**

These were considered and DEFERRED (not in scope for this plan, but listed here so they're not lost):

- **Trio session reuse (coder within an item)** — `trio/dispatcher.py:_run_coder` allocates a fresh session each round. The reviewer should stay fresh for independence, but the coder rounds for the same item could `--resume`. Larger blast radius — needs its own plan.
- **Per-phase skill scoping** — Claude Code loads all 27 project skills into every session's system reminder. Only `submit-intent-summary` is needed in the intent grill, etc. Needs investigation into whether `claude --print` respects a per-invocation `.claude/skills/` allowlist.
- **One-session-per-phase-type** (all 7 domain architects in one CLI session calling `submit-domain-adr` N times) — potentially huge savings but changes the "fresh context per domain" invariant; needs design discussion.

---

## Self-Review Notes (for the planner)

- All file paths checked against current repo state (head `1ac12cc` on `adr-019-project-secrets-vault`).
- `FINAL_VERIFICATION_SYSTEM` confirmed dead by grep; only reference outside the definition is a docstring comment in `validators.py:16`.
- `_INSPECT_UI_CACHE` is a module-level dict — acceptable for in-process scaffold runs (the parent driver lives in one process). If/when scaffold work is distributed, replace with a Redis-backed cache.
- Phase 0 (telemetry) is intentionally the first phase even though it doesn't save tokens — without it the rest of the plan can't be measured.
- Phases 1, 2, 3 touch overlapping scaffold modules. Tasks 2-7 are sequential by design (later tasks build on earlier ones' file shape). Task 8 (Phase 3) is independent and could parallelize, but to keep the merge-history clean it runs sequentially.
