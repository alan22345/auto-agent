# CLAUDE.md — Auto-Agent

## Build & Run Commands

| Task | Command |
|------|---------|
| Start locally | `python run.py` |
| Start with Docker | `docker compose up -d` |
| Run DB migrations | `docker compose exec auto-agent alembic upgrade head` |
| Lint | `ruff check .` |
| Lint (fix) | `ruff check --fix .` |
| Format | `ruff format .` |
| Format (check) | `ruff format --check .` |
| Unit tests | `.venv/bin/python3 -m pytest tests/ -q` |
| Run one test file | `.venv/bin/python3 -m pytest tests/test_microcompact.py -v` |
| Agent eval (vs CLI) | `cd eval && promptfoo eval --no-cache` |
| View eval results | `promptfoo view` |
| Regenerate TS types from Pydantic | `python3.12 scripts/gen_ts_types.py` |

## Architecture

### Dependency Layers (strict, top imports bottom only)

```
shared/types.py           # Pure Pydantic models
    ↑
shared/config.py          # Pydantic Settings — reads env (incl. AWS_BEARER_TOKEN_BEDROCK)
    ↑
shared/database.py        # SQLAlchemy async engine/session
shared/models.py          # ORM models
shared/redis_client.py    # Redis utilities
shared/events.py          # Event bus
shared/logging.py         # structlog setup
shared/notifier.py        # Notification helpers
shared/preflight.py       # Health checks
    ↑
agent/                    # Model-agnostic agent runtime (see below)
integrations/             # Slack, Telegram, Linear clients
    ↑
orchestrator/             # Task routing, classification, queue, webhooks, state machine
claude_runner/            # (legacy) Claude CLI execution loop — kept for pass-through mode
    ↑
web/                      # HTTP UI and static assets
    ↑
run.py                    # Entry point
```

### agent/ module layout

```
agent/
├─ loop.py                 # Core multi-turn tool-calling loop
├─ main.py                 # Event handlers: planning, coding, review, PO analysis
├─ classifier.py           # Task complexity classifier (simple/complex/complex-large)
├─ session.py              # Persistent session state across turns
├─ prompts.py              # Planning/coding/review prompt templates
├─ workspace.py            # Git workspace cloning and branch management
├─ harness.py              # One-off agent for onboarding repos (CLAUDE.md, lint, ADRs)
├─ po_analyzer.py          # Product Owner agent — generates improvement suggestions
│
├─ llm/
│   ├─ base.py             # Abstract LLMProvider interface
│   ├─ types.py            # Message, ToolCall, LLMResponse, TokenUsage
│   ├─ bedrock.py          # AWS Bedrock provider (default for VM deploys)
│   ├─ anthropic.py        # Native Anthropic API provider
│   └─ claude_cli.py       # Claude Code CLI pass-through (no tool loop)
│
├─ context/
│   ├─ __init__.py         # ContextManager — orchestrates all layers
│   ├─ system.py           # Builds system prompt (base + CLAUDE.md + git + repo map)
│   ├─ repo_map.py         # AST-based codebase index (Python + JS/TS)
│   ├─ workspace_state.py  # Tracks files read/modified/tested per session
│   ├─ microcompact.py     # Layer 1: clear old computed tool results
│   ├─ context_collapse.py # Layer 2: summarize old grep/glob groups
│   ├─ autocompact.py      # Layer 3: proactive full conversation summary
│   ├─ reactive_compact.py # Layer 4: 3-stage recovery on prompt_too_long
│   ├─ token_counter.py    # Token counting helpers
│   └─ attachments.py      # File re-attach after summarization
│
└─ tools/
    ├─ base.py             # Tool, ToolContext, ToolRegistry, ToolResult
    ├─ cache.py            # Result cache for glob/grep (invalidates on writes)
    ├─ file_read.py        # Read file (with offset/limit for large files)
    ├─ file_write.py       # Create/overwrite a file
    ├─ file_edit.py        # Precise string-replacement edit
    ├─ glob_tool.py        # File pattern matching (mtime-sorted)
    ├─ grep_tool.py        # Content search (regex + context lines + multiline)
    ├─ bash.py             # Shell execution (600s cap, output truncation)
    ├─ git.py              # Safe git wrapper (blocks push/reset-hard/etc.)
    └─ test_runner.py      # Structured test runner (auto-detects framework)
```

### Module Boundaries

| Directory | Owns | Must NOT import from |
|-----------|------|----------------------|
| `shared/` | Config, DB, models, types, events, logging | `agent/`, `orchestrator/`, `claude_runner/`, `integrations/`, `web/` |
| `agent/` | Model-agnostic runtime (loop, context, tools, llm) | `orchestrator/`, `web/`, `integrations/` |
| `integrations/` | External service clients | `agent/`, `orchestrator/`, `claude_runner/`, `web/` |
| `orchestrator/` | Task lifecycle, routing, webhooks, state machine | `claude_runner/`, `web/` |
| `claude_runner/` | Legacy Claude CLI subprocess runner | `web/` |
| `web/` | HTTP UI, static assets | _(can import from any lower layer)_ |
| `migrations/` | Alembic migrations | Everything except `shared/models`, `shared/database` |

### Critical invariants (context pipeline)

These are load-bearing — changing them has broken the agent before:

1. **`file_read` results are the agent's working memory.** Never collapse or clear them in microcompact or context_collapse. Clearing them triggers a re-read loop ("Groundhog Day" bug).
2. **Tool results for one assistant turn must be batched into one user message.** The LLM API requires `{role: user, content: [tool_result, tool_result, ...]}`, not multiple user messages with one `tool_result` each. See `_build_api_messages` in `agent/llm/bedrock.py` and `agent/llm/anthropic.py`.
3. **Only `grep`/`glob`/`git`/`bash` results are cleared by microcompact.** They're re-derivable; file contents are not.
4. **Transient Bedrock errors (503/429/529) are retried with exponential backoff** in `BedrockProvider.complete`. Do not remove.

### Key Design Patterns

- **Async everywhere**: All I/O uses `async/await` with `asyncio`
- **SQLAlchemy async sessions**: Use `async_session()` context manager from `shared/database.py`
- **Pydantic for validation**: All API inputs/outputs and inter-service data use Pydantic models in `shared/types.py`
- **Structured logging**: Use `structlog` via `shared/logging.py`, never bare `print()`
- **State machine**: Task status transitions go through `orchestrator/state_machine.py`
- **Redis for events**: Real-time updates flow through Redis pub/sub via `shared/events.py`
- **Fresh context per subtask**: Complex-large tasks decompose into phases; each phase runs with a fresh agent session (no state bleed between phases)

## Code Style

- **Naming**: snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE for constants
- **Imports**: stdlib → third-party → local, sorted alphabetically within groups
- **Error handling**: Let exceptions propagate unless there's a specific recovery action; log with context via `structlog`
- **No bare `print()`**: Use `structlog.get_logger()` for all output (except in debug-only scripts)
- **Pydantic models**: Define in `shared/types.py` for cross-module data; local models OK for single-module use
- **SQL models**: All ORM models live in `shared/models.py`
- **Async functions**: Prefix with `async` — never synchronous DB or HTTP calls in async contexts

## Testing

- **Unit tests** live in `tests/` — 150+ tests covering the agent runtime, tools, context pipeline, LLM providers, eval infrastructure
- **Integration eval** lives in `eval/` — promptfoo-based 10-task benchmark against Claude Code CLI
- Run the full unit suite before committing: `.venv/bin/python3 -m pytest tests/ -q`
- When fixing an agent bug, **write a failing test first** that reproduces the bug, then fix — the Groundhog Day bug slipped through twice because it was never caught by tests

## File Organization

- New integrations → `integrations/<service>/main.py`
- New API endpoints → `orchestrator/router.py`
- New webhook handlers → `orchestrator/webhooks/<source>.py`
- New Pydantic types → `shared/types.py`
- New ORM models → `shared/models.py`
- New agent tools → `agent/tools/<tool_name>.py` + register in `agent/tools/__init__.py`
- New LLM provider → `agent/llm/<provider>.py` + wire in `agent/llm/__init__.py::get_provider`
- New context layer → `agent/context/<layer>.py` + compose into `agent/context/__init__.py::prepare`
- Database migrations → `migrations/versions/` (use `alembic revision --autogenerate -m "description"`)
- Static web assets → `web/static/`
- Max file size guideline: ~500 lines. If a module exceeds this, split by concern.

## Methodology (MANDATORY)

Follow this process for every task. Do not skip steps. Do not jump to coding.

### Step 1: Understand the task
- Read the task title and description carefully.
- If anything is ambiguous, output `CLARIFICATION_NEEDED:` followed by your question, then STOP.
- If the task is clear, proceed.

### Step 2: Explore and plan
- Read the relevant files. Use the repo map above to find them — do not explore broadly.
- Think through 2-3 approaches. Pick the simplest one that fully solves the task.
- Write a brief plan before touching any code:
  - **Goal**: What this task accomplishes (2-3 sentences).
  - **Acceptance criteria**: Testable conditions that must be true when done.
  - **Files to modify**: Every file you will create or change, with a one-line note.
  - **Steps**: Numbered list of what you will do, in order.

### Step 3: Implement with TDD
For each change:
1. **Write a failing test** that captures the expected behavior.
2. **Run the test** — confirm it fails for the right reason.
3. **Write the minimal code** to make the test pass.
4. **Run the test** — confirm it passes.
5. **Commit** with a clear message.

If the repo has no test suite, skip TDD but still verify manually (run the app, check output).

### Step 4: Verify before claiming done
Before saying you are done:
1. Run the full test suite: `.venv/bin/python3 -m pytest tests/ -q`
2. Run the linter: `ruff check .`
3. Read the actual output. Confirm it passes.
4. If anything fails, fix it and re-run.

Do NOT say "should work" or "looks correct". Show the test/lint output as evidence.

### Step 5: Self-review
Run `git diff` and review your own changes:
- Off-by-one errors, missing edge cases, accidental debug code?
- Did you follow existing code style and patterns?
- Did you add anything the task didn't ask for? Remove it.
- For bug fixes: did you fix the root cause, not just the symptom?

### Step 6: Commit and report
- Commit with a message explaining what changed and why.
- For bug fixes, include the root cause in the commit message.

### Graph memory
After completing a task, check if anything notable was learned:
- Use `memory_search` to see if related knowledge exists.
- Use `memory_create_node` to record new decisions, capabilities, or preferences.
- Use `memory_append_decision` to update existing decisions (preserves history).
- If nothing notable was learned, skip this step.

## ADR process

Architectural decisions live in `docs/decisions/` as ADRs (Architecture Decision Records). Use `docs/decisions/000-template.md` as a template when making a non-obvious design choice. Examples in `docs/decisions/001-harness-engineering.md`.
