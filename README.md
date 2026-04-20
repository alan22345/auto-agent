# Auto-Agent

Autonomous AI coding agent and **team collaboration platform**. Ingests tasks, plans, codes, runs tests, and opens PRs — all driven by a **model-agnostic agent runtime** with pluggable LLM providers (AWS Bedrock, Anthropic API, Claude Code CLI) and a tuned context pipeline for long-running coherence.

Shared **graph memory** lets the agent learn your team's preferences, project decisions, and cross-project capabilities — so it gets smarter over time.

## What it does

Give it a description of work and it runs the whole cycle autonomously:

```
intake → classify → [plan → human approval] → code → test → self-review
      → PR → CI → independent review → merge
```

Tasks arrive from Slack, Telegram, Linear, GitHub webhooks, or the web UI. Long-running complex work is broken into subtasks with fresh agent context per subtask. A separate **PO (Product Owner) agent** runs on a cron per-repo, analyses the codebase, and proposes improvement tasks.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Intake: Slack · Telegram · Linear · GitHub webhooks · Web UI  │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
                    orchestrator/         FastAPI + Redis pub/sub
                    ├─ state machine      (task lifecycle, queue, routing)
                    └─ auth               (JWT sessions, per-user login)
                           │
                           ▼
                     agent/               Model-agnostic agent runtime
                     ├─ loop.py           (multi-turn tool-calling loop)
                     ├─ context/          (4-layer context pipeline)
                     │   ├─ repo_map         — AST-based codebase index
                     │   ├─ microcompact     — clear stale tool results
                     │   ├─ context_collapse — group search ops
                     │   ├─ autocompact      — proactive summarization
                     │   ├─ memory           — graph memory injection
                     │   └─ workspace_state  — track reads/writes
                     ├─ tools/            (file_read/write/edit, glob, grep,
                     │                     bash, git, test_runner,
                     │                     memory_read, memory_write)
                     ├─ llm/              (Bedrock · Anthropic · CLI)
                     └─ main.py           (planning/coding/review phases)
                           │
                           ▼
                    Git workspace clones → PR → CI → merge
```

**Infrastructure:** FastAPI on port 2020, PostgreSQL (tasks, repos, users, graph memory), Redis (event streaming), Docker Compose.

## Key features

### Team collaboration
- **Per-user authentication** — username/password login with JWT sessions
- **Shared visibility** — all team members see all tasks, messages, and agent activity
- **User attribution** — messages and tasks show who created/sent them
- **Full shared access** — any team member can approve, reject, or guide any task

### Graph memory
- **Shared knowledge graph** stored in PostgreSQL (nodes + edges adjacency list)
- **LLM-generated node names and relations** — the agent picks names that make semantic sense to it
- **Append-only decision chains** — decisions are never overwritten, they chain via `evolved-from` edges preserving full history
- **Cross-project intelligence** — projects advertise capabilities they produce/consume, so the agent discovers connections (e.g., "data-generator produces financial-reports" → new UI project can consume them)
- **Automatic context injection** — relevant memory is queried and injected into the system prompt at task start
- **Post-task reflection** — after completing work, the agent records decisions, capabilities, and preferences to the graph

### Agent runtime
- **Persistent repo map** — AST-based file/class/function index stored in graph memory, built once per repo and incrementally updated via `git diff` on subsequent tasks (no full rebuild unless history is rewritten)
- **Verification gate** — detects when the agent finishes without running tests, injects a nudge to verify before claiming completion
- **Exploration budget** — if the agent spends too many turns reading without writing, a nudge prompts it to start implementing
- **Workspace state tracker** — flags redundant re-reads of the same file
- **Tool result caching** — same-session dedup for `glob`/`grep` results
- **Structured test runner** — auto-detects pytest/jest/vitest/mocha/go/cargo/rspec, parses results, returns structured pass/fail summary
- **Multi-agent subtask execution** — complex-large plans decompose into phases, each runs with a fresh agent context

### Model routing
- **Three tiers**: `fast` (Haiku — mechanical tasks), `standard` (Sonnet — implementation), `capable` (Opus — architecture)
- Repo summarization + plan review use the fast tier; coding uses standard

### Freeform mode
PO agent analyzes a repo on cron, generates 3-5 improvement suggestions, optionally auto-approves them, creates tasks, runs them through the pipeline, auto-merges to a dev branch after CI passes. You promote good changes to main or revert from dev.

### Deployment
- **Bedrock bearer token auth** (`AWS_BEARER_TOKEN_BEDROCK`) — works on any VM without IAM setup
- Docker Compose for everything (postgres + redis + app)
- Health check at `/health`

## Quick start (local)

```bash
git clone https://github.com/<owner>/auto-agent.git
cd auto-agent
cp .env.example .env   # fill in GITHUB_TOKEN, LLM_PROVIDER, ADMIN_PASSWORD, etc.
docker compose up -d
docker compose exec auto-agent alembic upgrade head   # first time only
open http://localhost:2020
```

Log in with the admin credentials you set in `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`). Create additional users from the UI.

See [SETUP.md](SETUP.md) for full setup (GitHub PAT, Telegram bot, Bedrock auth).

## Eval suite

10-test benchmark comparing Auto-Agent against Claude Code CLI on the same tasks:

| Category | Tests |
|----------|-------|
| Bug fixes | off-by-one pagination, case-insensitive search |
| Features | retry decorator, LRU cache |
| Refactoring | extract duplicated validation |
| Testing | write Jest tests for calculator |
| Multi-file features | Flask preferences (model + routes + tests) |
| Cross-module bugs | root-cause analysis across modules |
| Architecture | choose polling vs event-signaling, justify trade-offs |
| Migration | sync → async API client with backwards compat |
| Perf vs readability | optimize pipeline without breaking clarity |

```bash
# Run the eval
cd eval && promptfoo eval --no-cache
promptfoo view   # browse results in browser
```

Results and assertions live in `eval/`. See [eval/README.md](eval/README.md) _(coming)_ for details on the 10 test cases and scoring.

## Task lifecycle

```
Ingested → Classified (simple/complex/complex-large) → Queued
  → [Complex: Planning → Awaiting Approval →] Coding
  → Self-Review → PR Created → CI Checking
  → Independent Review → Human Review → Done
```

**Complex-large tasks** are decomposed into `## Phase N` blocks; each phase runs with a fresh agent session (context isolation pattern from superpowers methodology).

## Repo layout

| Path | Purpose |
|------|---------|
| `agent/` | Model-agnostic agent: loop, context pipeline, tools, LLM providers, graph memory |
| `orchestrator/` | Task lifecycle, routing, webhooks, state machine, auth |
| `integrations/` | External service clients (Slack, Telegram, Linear) |
| `shared/` | Config, DB models (users, tasks, repos, memory graph), types, event bus |
| `web/` | HTTP UI + static assets (login, task management, pair-programming) |
| `eval/` | Promptfoo eval suite (10 tests + custom assertions + fixtures) |
| `tests/` | Unit tests for the agent runtime (230+ tests) |
| `migrations/` | Alembic DB migrations |
| `docs/decisions/` | Architecture Decision Records |

## Contributing

See [CLAUDE.md](CLAUDE.md) for build/lint/test commands, architecture rules (layer imports, module boundaries), and code style.

**Run tests before committing:**

```bash
.venv/bin/python3 -m pytest tests/ -q
ruff check . && ruff format --check .
```
