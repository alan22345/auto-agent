# Next Iteration: Beat Claude Code CLI on Bedrock

## Context

We built a model-agnostic coding agent (`agent/`) that replaces the Claude Code CLI dependency. First eval on 2026-04-15 shows **Agent 0.66 avg vs CLI 0.71 avg** (both 3/5 pass). The agent scores *higher* on tasks it completes (0.90+ vs 0.80) but fails 2/5 due to inefficiency. All code is on branch `feature/model-agnostic-agent`.

## What Needs to Happen

### 1. Fix the Over-Exploration Problem (highest priority)

The agent's biggest failure mode is spending too many tool calls reading files before doing anything. Test 2 (add retry decorator): 49 tool calls in 70s, never wrote the decorator. The CLI did it in 20s.

**Root cause:** The system prompt tells the agent to "explore the codebase" and "read README and CLAUDE.md" before coding — but for a simple single-file task, this is wasteful. The agent reads every file it can find before acting.

**Fix approaches:**
- Add a "task complexity heuristic" to the system prompt: for single-file tasks with clear instructions, skip exploration and go straight to implementation
- Cap exploration turns: after N read-only tool calls without any writes, inject a nudge message ("You've explored enough — start implementing")
- Reduce the default `max_turns` for simple tasks (currently 50, try 20)
- Tune the coding prompt in `agent/prompts.py` to be more directive: "Implement immediately. Only explore if you don't know where to make changes."

**Files to modify:**
- `agent/context/system.py` — system prompt methodology section
- `agent/prompts.py` — CODING_PROMPT is too permissive about exploration
- `agent/loop.py` — add an exploration budget / nudge mechanism

### 2. Fix Context Token Overflow

Test 4 (write JS tests): Agent hit `ValidationException: prompt is too long: 10386440 tokens > 1000000 maximum`. This happened during the LLM-rubric grading call, not the agent itself, but it signals that the agent's output JSON (which includes full file contents) is too large.

**Fix approaches:**
- Truncate file contents in the provider output JSON (cap each file at 5000 chars)
- Don't include unmodified files in the output — only return files that changed
- The `agent/context/autocompact.py` thresholds may need tuning for Sonnet 4.6's 200K context

**Files to modify:**
- `eval/providers/agent_provider.py` — truncate output, only include changed files
- `agent/context/autocompact.py` — verify thresholds match Bedrock's actual limits

### 3. Write Harder Tests

The current 5 tests are too simple — all single-file tasks on small fixtures. Real coding work involves multi-file changes, understanding existing architecture, running test suites, and debugging failures.

**New test categories to add:**

**Multi-file feature (medium):**
- Fixture: A Flask/FastAPI app with routes, models, and tests
- Task: "Add a new API endpoint for user preferences with validation, database model, and tests"
- Assertions: new route exists, model added, tests pass, existing tests still pass

**Cross-file bug (hard):**
- Fixture: A project where a bug in module A manifests as a failure in module B's tests
- Task: "The tests in test_orders.py are failing. Fix the root cause."
- Assertions: root cause fixed (not the symptom), all tests pass

**Dependency upgrade (hard):**
- Fixture: A project using an old API pattern
- Task: "Upgrade the database calls from sync to async using SQLAlchemy 2.0 patterns"
- Assertions: all files converted, no sync calls remain, tests pass

**Full TDD cycle (hard):**
- Fixture: A project with a requirements spec but no implementation
- Task: "Implement the user authentication module following TDD. Write failing tests first, then implement."
- Assertions: tests exist before implementation commits, all tests pass, auth works

**Real-world repo test (hardest):**
- Use an actual open-source repo (clone a small one) as the fixture
- Task: a real GitHub issue from that repo
- Assertions: issue is resolved, tests pass, code review passes

**Files to create:**
- `eval/fixtures/flask-app/` — multi-file Flask app fixture
- `eval/fixtures/cross-file-bug/` — bug that crosses module boundaries
- New test entries in `eval/promptfooconfig.yaml`

### 4. Speed Optimization

Agent is 3-5x slower than CLI. Some of this is inherent (tool calling round-trips) but some is waste.

**Approaches:**
- Batch tool calls: if the agent wants to read 3 files, make all 3 API calls in one LLM turn (the agent already supports multiple tool_calls per turn — verify Bedrock returns them)
- Reduce system prompt size: the superpowers methodology section is ~800 tokens. Consider making it conditional (only inject for complex tasks)
- Use Haiku for simple sub-tasks (slugify, PR title generation) instead of Sonnet

### 5. Eval Infrastructure Improvements

- Add timing assertions: agent should complete simple tasks in <60s
- Add token efficiency metric: tokens per line of code changed
- Add a "did it commit?" assertion (separate from diff detection)
- Track cost per test (Bedrock pricing)

## Architecture Reference

```
agent/
  llm/
    bedrock.py          # Bedrock provider (Sonnet 4.6: us.anthropic.claude-sonnet-4-6)
    anthropic.py        # Direct API provider
    claude_cli.py       # CLI pass-through for A/B comparison
  tools/                # 7 tools: file_read/write/edit, glob, grep, bash, git
  context/              # 4-layer compaction: microcompact → collapse → autocompact → reactive
  loop.py               # Core agentic loop (modify for exploration budget)
  prompts.py            # Prompt templates (modify for efficiency)
  main.py               # Event loop (wired into run.py)
eval/
  promptfooconfig.yaml  # Test definitions
  providers/            # agent_provider.py + claude_cli_provider.py
  fixtures/             # Test codebases
  assertions/           # Custom Python assertions
superpowers/            # Git submodule: obra/superpowers methodology
```

## Run the Eval

```bash
# Install deps (one-time)
uv pip install "anthropic[bedrock]" "botocore[crt]" --python .venv/bin/python3
npm install -g promptfoo

# Verify Bedrock auth
aws sts get-caller-identity

# Run eval
cd eval && promptfoo eval --no-cache

# View results in browser
promptfoo view
```

## Success Criteria

The agent should:
1. Pass all 5 existing tests (currently 3/5)
2. Score ≥0.80 avg across all tests (currently 0.66)
3. Complete simple tasks in <60s (currently 70-150s)
4. Pass at least 3/5 of the new harder tests
5. Beat Claude CLI on overall average score
