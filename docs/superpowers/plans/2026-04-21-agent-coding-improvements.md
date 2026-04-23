# Agent Coding Quality Improvements

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the agent's ability to correctly understand and execute user commands by adding structured intent extraction, better prompts, complexity-aware exploration budgets, and richer inter-phase context.

**Architecture:** Six targeted changes across four files: add an intent extraction step in `agent/main.py` that runs a quick LLM call after classification, restructure prompts in `agent/prompts.py` to require structured planning output and differentiate with/without plan coding, make the exploration budget in `agent/loop.py` complexity-aware, and enrich subtask handoff context in `agent/main.py`.

**Tech Stack:** Python, async/await, existing LLM provider infrastructure, Pydantic models

---

### Task 1: Add structured intent fields to TaskData

**Files:**
- Modify: `shared/types.py:35-53` (TaskData model)
- Test: `tests/test_intent_extraction.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intent_extraction.py
"""Tests for intent extraction and structured intent fields."""

from __future__ import annotations

import pytest
from shared.types import TaskData


class TestTaskDataIntentFields:
    def test_intent_fields_have_defaults(self):
        """New intent fields should be optional with None defaults."""
        task = TaskData(
            id=1, title="Fix login bug", description="Login fails on mobile",
            source="manual", status="created",
        )
        assert task.change_type is None
        assert task.target_areas is None
        assert task.acceptance_criteria is None
        assert task.constraints is None

    def test_intent_fields_populated(self):
        """Intent fields can be set explicitly."""
        task = TaskData(
            id=1, title="Fix login bug", description="Login fails on mobile",
            source="manual", status="created",
            change_type="bugfix",
            target_areas="auth/login.py, auth/session.py",
            acceptance_criteria="Login works on mobile browsers, existing tests pass",
            constraints="Do not change the session schema",
        )
        assert task.change_type == "bugfix"
        assert task.target_areas == "auth/login.py, auth/session.py"
        assert task.acceptance_criteria == "Login works on mobile browsers, existing tests pass"
        assert task.constraints == "Do not change the session schema"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestTaskDataIntentFields -v`
Expected: FAIL with `AttributeError` — fields don't exist on TaskData yet.

- [ ] **Step 3: Add intent fields to TaskData**

In `shared/types.py`, add four new optional fields to `TaskData` after `current_subtask`:

```python
    # Structured intent (extracted by LLM after classification)
    change_type: str | None = None          # "bugfix", "feature", "refactor", "config", "docs"
    target_areas: str | None = None         # comma-separated file paths or module areas
    acceptance_criteria: str | None = None   # what "done" looks like
    constraints: str | None = None          # what NOT to do
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestTaskDataIntentFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/types.py tests/test_intent_extraction.py
git commit -m "feat: add structured intent fields to TaskData"
```

---

### Task 2: Implement intent extraction LLM call

**Files:**
- Modify: `agent/main.py` (add `extract_intent` function after the helpers section, ~line 165)
- Test: `tests/test_intent_extraction.py` (append to existing)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_intent_extraction.py`:

```python
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from agent.main import extract_intent


class TestExtractIntent:
    @pytest.mark.asyncio
    async def test_extract_intent_parses_json(self):
        """extract_intent should return parsed intent fields from LLM JSON output."""
        mock_response = MagicMock()
        mock_response.message.content = json.dumps({
            "change_type": "bugfix",
            "target_areas": "auth/login.py",
            "acceptance_criteria": "Login works on mobile",
            "constraints": "Don't change session schema",
        })

        mock_provider = AsyncMock()
        mock_provider.complete.return_value = mock_response

        with patch("agent.main.get_provider", return_value=mock_provider):
            result = await extract_intent("Fix login bug", "Login fails on mobile devices")

        assert result["change_type"] == "bugfix"
        assert result["target_areas"] == "auth/login.py"
        assert result["acceptance_criteria"] == "Login works on mobile"
        assert result["constraints"] == "Don't change session schema"

    @pytest.mark.asyncio
    async def test_extract_intent_returns_empty_on_failure(self):
        """If LLM call fails, return empty dict — don't block the pipeline."""
        mock_provider = AsyncMock()
        mock_provider.complete.side_effect = Exception("LLM down")

        with patch("agent.main.get_provider", return_value=mock_provider):
            result = await extract_intent("Fix login bug", "Login fails on mobile")

        assert result == {}

    @pytest.mark.asyncio
    async def test_extract_intent_returns_empty_on_invalid_json(self):
        """If LLM returns non-JSON, return empty dict."""
        mock_response = MagicMock()
        mock_response.message.content = "I think this is a bugfix for the login page"

        mock_provider = AsyncMock()
        mock_provider.complete.return_value = mock_response

        with patch("agent.main.get_provider", return_value=mock_provider):
            result = await extract_intent("Fix login bug", "Login fails on mobile")

        assert result == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestExtractIntent -v`
Expected: FAIL with `ImportError` — `extract_intent` doesn't exist yet.

- [ ] **Step 3: Implement extract_intent in agent/main.py**

Add after the `_trim_plan_text` function (~line 163), before the agent factory section:

```python
# ---------------------------------------------------------------------------
# Intent extraction — structured understanding of what the user wants
# ---------------------------------------------------------------------------

INTENT_EXTRACTION_PROMPT = """\
Analyze this task and extract structured intent. Output ONLY a JSON object, no other text.

Task title: {title}
Task description: {description}

JSON format:
{{
  "change_type": "bugfix|feature|refactor|config|docs|test|performance",
  "target_areas": "comma-separated file paths or module areas likely involved",
  "acceptance_criteria": "what must be true when the task is done (1-2 sentences)",
  "constraints": "what should NOT be changed or any restrictions (1 sentence, or empty string)"
}}

Rules:
- change_type: pick the single best category
- target_areas: infer from the description — name specific files/modules if mentioned, otherwise name the likely area (e.g. "authentication", "database layer")
- acceptance_criteria: concrete, testable conditions — not vague ("works correctly")
- constraints: only include if the description implies restrictions; empty string otherwise
- Output ONLY the JSON. No markdown fences, no explanation.
"""


async def extract_intent(title: str, description: str) -> dict:
    """Extract structured intent from a task title and description.

    Returns a dict with keys: change_type, target_areas, acceptance_criteria, constraints.
    Returns empty dict on any failure (non-blocking — the pipeline continues without it).
    """
    import json as _json
    try:
        provider = get_provider(model_override="fast")
        from agent.llm.types import Message
        response = await provider.complete(
            messages=[Message(
                role="user",
                content=INTENT_EXTRACTION_PROMPT.format(title=title, description=description),
            )],
            max_tokens=300,
        )
        text = response.message.content.strip()
        # Strip markdown fences if the LLM wraps the JSON
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        return _json.loads(text)
    except Exception:
        log.warning("intent_extraction_failed", title=title[:80])
        return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestExtractIntent -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/main.py tests/test_intent_extraction.py
git commit -m "feat: add extract_intent LLM call for structured task understanding"
```

---

### Task 3: Wire intent extraction into the coding pipeline

**Files:**
- Modify: `agent/main.py:614-700` (handle_coding and _handle_coding_single)
- Modify: `agent/prompts.py:31-96,335-349` (CODING_PROMPT and build_coding_prompt)
- Test: `tests/test_intent_extraction.py` (append)

- [ ] **Step 1: Write the failing test for intent in coding prompt**

Append to `tests/test_intent_extraction.py`:

```python
from agent.prompts import build_coding_prompt


class TestCodingPromptWithIntent:
    def test_coding_prompt_includes_intent_section(self):
        """When intent dict is provided, the coding prompt should include a structured intent section."""
        intent = {
            "change_type": "bugfix",
            "target_areas": "auth/login.py",
            "acceptance_criteria": "Login works on mobile",
            "constraints": "Don't change session schema",
        }
        prompt = build_coding_prompt(
            title="Fix login bug",
            description="Login fails on mobile",
            intent=intent,
        )
        assert "## Structured intent" in prompt
        assert "bugfix" in prompt
        assert "auth/login.py" in prompt
        assert "Login works on mobile" in prompt
        assert "Don't change session schema" in prompt

    def test_coding_prompt_without_intent(self):
        """When no intent is provided, the prompt should still work without an intent section."""
        prompt = build_coding_prompt(
            title="Fix login bug",
            description="Login fails on mobile",
        )
        assert "## Structured intent" not in prompt
        assert "## Task" in prompt

    def test_coding_prompt_without_plan_has_restate_step(self):
        """When no plan is provided, the coding prompt should ask the agent to restate the task."""
        prompt = build_coding_prompt(
            title="Fix login bug",
            description="Login fails on mobile",
        )
        assert "restate" in prompt.lower() or "summarize what you" in prompt.lower()

    def test_coding_prompt_with_plan_no_restate(self):
        """When a plan is provided, the coding prompt should NOT ask to restate."""
        prompt = build_coding_prompt(
            title="Fix login bug",
            description="Login fails on mobile",
            plan="## Phase 1\nDo the thing",
        )
        # With a plan, the agent should implement immediately
        assert "IMMEDIATELY" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestCodingPromptWithIntent -v`
Expected: FAIL — `build_coding_prompt` doesn't accept `intent` parameter yet.

- [ ] **Step 3: Update CODING_PROMPT and build_coding_prompt in prompts.py**

Replace the `CODING_PROMPT` template (lines 31-96) with two variants:

```python
# Coding prompt when a plan exists — implement immediately
CODING_PROMPT_WITH_PLAN = """\
## Instructions
Implement the task below IMMEDIATELY. Do NOT explore the codebase broadly — only \
read files directly relevant to the change. If the task specifies which file(s) to \
modify, go straight to editing. If unclear, do ONE targeted search then start coding.

1. Implement the task following the repo's existing patterns and style.
2. Run the test suite and fix any failures you introduce.
3. Self-review your changes before committing.
{clarification_instructions}
## Task
Title: {title}
Description: {description}
{intent_section}
{plan_section}

{critical_rules}
"""

# Coding prompt when NO plan exists — understand first, then code
CODING_PROMPT_NO_PLAN = """\
## Instructions
Before writing any code, briefly summarize what you are about to do in 2-3 sentences. \
State which files you expect to modify and what the end result should look like. \
Then implement.

1. Restate the task: what are you changing, where, and what does "done" look like?
2. Do ONE targeted search to find the relevant code.
3. Implement following the repo's existing patterns and style.
4. Run the test suite and fix any failures you introduce.
5. Self-review your changes before committing.
{clarification_instructions}
## Task
Title: {title}
Description: {description}
{intent_section}

{critical_rules}
"""

# Shared critical rules (extracted from the original CODING_PROMPT)
_CRITICAL_RULES = """\
## Critical rules

### Root-cause analysis (MANDATORY for bug fixes)
- If this is a bug fix, you MUST identify and fix the ROOT CAUSE, not just the symptom.
- Trace the bug back to where the incorrect behavior originates.
- Do NOT apply band-aid fixes (e.g., adding a null check at the crash site when the real issue is that null should never have been passed).
- If you find that the root cause is in a different area than expected, fix it at the source.
- Document your root-cause analysis in the commit message.

### Code quality
- Follow existing code style and patterns in the repo.
- Do not introduce new dependencies unless absolutely necessary.
- Do not refactor unrelated code — keep changes focused on the task.
- Ensure all existing tests still pass.
- Add tests for new functionality if the repo has a test suite.
- When replacing a function, REMOVE the old version entirely — never leave duplicate definitions.

### Security
- No hardcoded secrets, tokens, or credentials.
- Validate inputs at system boundaries.
- No SQL injection, XSS, or command injection vulnerabilities.

### Architecture Decision Records (ADRs)
For any non-trivial change — new components, new patterns, dependency additions, \
significant design trade-offs, API changes — create an ADR in `docs/decisions/`. \
If the directory doesn't exist, create it. Use this format:

```
# NNN — Short title of the decision

## Status
Accepted

## Context
What prompted this decision? What problem were you solving?

## Decision
What did you decide and why?

## Consequences
What trade-offs does this introduce? What are the alternatives you rejected?
```

Number sequentially (check existing files). Skip ADRs for trivial changes \
(typo fixes, version bumps, config tweaks).

## After implementation
{ci_checks_section}
1. Review your own diff — look for: off-by-one errors, missing edge cases, accidental debug code, incomplete error handling.
2. Commit with a clear message: what changed, why, and (for bug fixes) what the root cause was."""
```

- [ ] **Step 4: Update the build_coding_prompt function**

Replace `build_coding_prompt` (lines 335-349):

```python
def _intent_section(intent: dict | None) -> str:
    if not intent:
        return ""
    parts = ["## Structured intent"]
    if intent.get("change_type"):
        parts.append(f"- **Type:** {intent['change_type']}")
    if intent.get("target_areas"):
        parts.append(f"- **Target areas:** {intent['target_areas']}")
    if intent.get("acceptance_criteria"):
        parts.append(f"- **Done when:** {intent['acceptance_criteria']}")
    if intent.get("constraints"):
        parts.append(f"- **Constraints:** {intent['constraints']}")
    return "\n".join(parts)


def build_coding_prompt(
    title: str,
    description: str,
    plan: str | None = None,
    repo_summary: str | None = None,
    ci_checks: str | None = None,
    intent: dict | None = None,
) -> str:
    intent_section = _intent_section(intent)
    critical_rules = _CRITICAL_RULES.format(ci_checks_section=_ci_checks_section(ci_checks))

    if plan:
        plan_section = f"\n## Approved plan\n{plan}\n"
        result = CODING_PROMPT_WITH_PLAN.format(
            title=title,
            description=description,
            plan_section=plan_section,
            intent_section=intent_section,
            clarification_instructions=CLARIFICATION_INSTRUCTIONS,
            critical_rules=critical_rules,
        )
    else:
        result = CODING_PROMPT_NO_PLAN.format(
            title=title,
            description=description,
            intent_section=intent_section,
            clarification_instructions=CLARIFICATION_INSTRUCTIONS,
            critical_rules=critical_rules,
        )
    return result + _repo_context(repo_summary)
```

- [ ] **Step 5: Wire intent extraction into handle_coding in main.py**

In `handle_coding` (agent/main.py), after fetching the task (~line 616) and before the coding path split (~line 668), add intent extraction:

```python
    # Extract structured intent (fast LLM call — non-blocking on failure)
    intent = await extract_intent(task.title, task.description)
    if intent:
        log.info(f"Intent extracted for task #{task_id}: {intent.get('change_type', '?')}")
```

Then pass `intent` to both coding paths. In `_handle_coding_single`, update the `build_coding_prompt` call:

```python
    coding_prompt = build_coding_prompt(
        task.title, task.description, task.plan, repo.summary, repo.ci_checks, intent=intent,
    )
```

And update `_handle_coding_single` signature to accept `intent: dict | None = None`.

Similarly in `_handle_coding_with_subtasks`, pass `intent` through and include it in the subtask prompts.

- [ ] **Step 6: Run tests to verify**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add agent/prompts.py agent/main.py tests/test_intent_extraction.py
git commit -m "feat: wire intent extraction into coding pipeline, split coding prompt by plan presence"
```

---

### Task 4: Add structured output format to planning prompt

**Files:**
- Modify: `agent/prompts.py:16-29` (PLANNING_PROMPT)
- Test: `tests/test_intent_extraction.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_intent_extraction.py`:

```python
from agent.prompts import build_planning_prompt


class TestStructuredPlanningPrompt:
    def test_planning_prompt_requires_goal_section(self):
        """Planning prompt should instruct agent to include a Goal section."""
        prompt = build_planning_prompt("Add dark mode", "Support dark mode toggle in settings")
        assert "## Goal" in prompt

    def test_planning_prompt_requires_acceptance_criteria(self):
        """Planning prompt should instruct agent to include acceptance criteria."""
        prompt = build_planning_prompt("Add dark mode", "Support dark mode toggle in settings")
        assert "acceptance criteria" in prompt.lower() or "Acceptance Criteria" in prompt

    def test_planning_prompt_requires_files_to_modify(self):
        """Planning prompt should instruct agent to list files to modify."""
        prompt = build_planning_prompt("Add dark mode", "Support dark mode toggle in settings")
        assert "files" in prompt.lower() and "modify" in prompt.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestStructuredPlanningPrompt -v`
Expected: FAIL — current planning prompt doesn't have these structured sections.

- [ ] **Step 3: Update PLANNING_PROMPT in prompts.py**

Replace `PLANNING_PROMPT` (lines 16-29):

```python
PLANNING_PROMPT = """\
## Instructions
1. Read the README and CLAUDE.md (if they exist) to understand repo conventions, tech stack, and patterns.
2. Explore the codebase to understand the relevant areas.
3. Create a detailed implementation plan using the structure below.

IMPORTANT: Output the plan as plain text in your response. Do NOT write to any files. Just print the plan directly.
{clarification_instructions}
## Task
Title: {title}
Description: {description}

## Required plan structure
Your plan MUST include these sections in this order:

### Goal
Restate what this task accomplishes in 2-3 concrete sentences. Prove you understand the user's intent.

### Acceptance Criteria
Bullet list of testable conditions that must be true when the task is done. Be specific — \
"works correctly" is not a criterion; "login succeeds on mobile Safari with valid credentials" is.

### Files to Modify
List every file you expect to create or modify, with a one-line note on what changes:
- `path/to/file.py` — add new endpoint for X
- `path/to/test.py` — add test coverage for X

### Implementation Phases
Break the work into sequential phases (## Phase 1, ## Phase 2, etc.). Each phase should:
- Have a clear, descriptive title
- List the specific changes to make
- Be independently committable

Do NOT write any code. Plan only. Output the plan as text.
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestStructuredPlanningPrompt -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/prompts.py tests/test_intent_extraction.py
git commit -m "feat: add structured output format to planning prompt (Goal, Acceptance Criteria, Files)"
```

---

### Task 5: Make exploration budget complexity-aware

**Files:**
- Modify: `agent/loop.py:32,76-91,149-161,387-405` (budget constant → per-instance, constructor, _run_agentic)
- Test: `tests/test_loop_behavior.py` (modify existing tests, add new)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loop_behavior.py`:

```python
class TestComplexityAwareExplorationBudget:
    def test_default_budget_unchanged(self):
        """Default budget (no complexity) should be 8."""
        from agent.loop import _EXPLORATION_BUDGET
        assert _EXPLORATION_BUDGET == 8

    def test_get_exploration_budget_simple(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget("simple") == 5

    def test_get_exploration_budget_complex(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget("complex") == 15

    def test_get_exploration_budget_complex_large(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget("complex_large") == 25

    def test_get_exploration_budget_none_returns_default(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget(None) == 8

    def test_get_exploration_budget_unknown_returns_default(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget("unknown") == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_loop_behavior.py::TestComplexityAwareExplorationBudget -v`
Expected: FAIL — `get_exploration_budget` doesn't exist.

- [ ] **Step 3: Add get_exploration_budget and wire into AgentLoop**

In `agent/loop.py`, add the budget lookup function after the constants (~line 37):

```python
_COMPLEXITY_BUDGETS = {
    "simple": 5,
    "complex": 15,
    "complex_large": 25,
}


def get_exploration_budget(complexity: str | None) -> int:
    """Return the exploration budget for a given task complexity."""
    if complexity is None:
        return _EXPLORATION_BUDGET
    return _COMPLEXITY_BUDGETS.get(complexity, _EXPLORATION_BUDGET)
```

Add `complexity: str | None = None` parameter to `AgentLoop.__init__` (after `repo_name`):

```python
    self._complexity = complexity
```

In `_run_agentic`, replace the hardcoded budget check (line 396):

```python
                budget = get_exploration_budget(self._complexity)
                if consecutive_reads >= budget and not nudge_injected:
```

- [ ] **Step 4: Pass complexity through _create_agent in main.py**

Add `complexity: str | None = None` parameter to `_create_agent` and pass it to `AgentLoop`:

In `_create_agent` signature:
```python
def _create_agent(
    workspace: str,
    ...
    repo_name: str | None = None,
    complexity: str | None = None,
) -> AgentLoop:
```

Pass to AgentLoop:
```python
    return AgentLoop(
        ...
        repo_name=repo_name,
        complexity=complexity,
    )
```

In `_handle_coding_single`, pass complexity:
```python
    agent = _create_agent(
        workspace, session_id=session_id, max_turns=50,
        task_id=task_id, task_description=task.description,
        repo_name=repo.name, complexity=task.complexity,
    )
```

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/python3 -m pytest tests/test_loop_behavior.py -v`
Expected: ALL PASS (including existing tests — the default budget is unchanged)

- [ ] **Step 6: Commit**

```bash
git add agent/loop.py agent/main.py tests/test_loop_behavior.py
git commit -m "feat: make exploration budget complexity-aware (5/15/25 for simple/complex/complex_large)"
```

---

### Task 6: Enrich subtask handoff context

**Files:**
- Modify: `agent/main.py:787-798` (subtask prompt for phases after the first)
- Modify: `agent/main.py:827-828` (output_preview length)
- Test: `tests/test_intent_extraction.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_intent_extraction.py`:

```python
class TestSubtaskHandoff:
    def test_output_preview_length_at_least_1000(self):
        """Subtask output_preview should capture at least 1000 chars, not just 200."""
        # This tests the constant used when saving phase output
        # We verify by checking the slice used in the code
        import ast
        import inspect
        from agent import main as agent_main

        source = inspect.getsource(agent_main._handle_coding_with_subtasks)
        # The old code had output[:200]; we need output[:1000] or more
        assert "output[:200]" not in source, "output_preview should be longer than 200 chars"
        assert "output[:1000]" in source or "output[:1500]" in source or "output[:2000]" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestSubtaskHandoff -v`
Expected: FAIL — source still contains `output[:200]`.

- [ ] **Step 3: Increase output_preview and enrich handoff prompt**

In `agent/main.py`, line 828, change:
```python
        phases[i]["output_preview"] = output[:200]
```
to:
```python
        phases[i]["output_preview"] = output[:1500]
```

In the subtask prompt construction (lines 787-798), replace the previous-phase context block:

```python
        else:
            # Provide context about what previous phases did (fresh context pattern)
            prev_summaries = []
            for j in range(i):
                title = phases[j].get("title", f"Phase {j + 1}")
                preview = phases[j].get("output_preview", "completed")
                prev_summaries.append(f"### Phase {j + 1}: {title}\n{preview}")
            prev_context = "\n\n".join(prev_summaries)

            # Also include a git diff stat so the agent knows what files were touched
            prompt += (
                f"## Previous phases (already implemented — do NOT redo)\n{prev_context}\n\n"
                "Run `git log --oneline -10` and `git diff --stat HEAD~5` to see what previous phases changed.\n\n"
                "Implement ONLY the current phase. Commit your changes before stopping.\n"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_intent_extraction.py::TestSubtaskHandoff -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/main.py tests/test_intent_extraction.py
git commit -m "feat: enrich subtask handoff — 1500-char previews, git context instructions"
```

---

### Task 7: Run full test suite and lint

**Files:**
- No new files

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: All tests pass, including new tests from this plan.

- [ ] **Step 2: Run linter**

Run: `ruff check .`
Expected: No errors.

- [ ] **Step 3: Run formatter check**

Run: `ruff format --check .`
Expected: No formatting issues.

- [ ] **Step 4: Fix any issues found in steps 1-3**

If tests fail or lint errors exist, fix them and re-run.

- [ ] **Step 5: Final commit if needed**

```bash
git add -u
git commit -m "fix: address lint/test issues from agent coding improvements"
```
