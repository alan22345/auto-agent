# CLAUDE.md — Auto-Agent

## Build & Run Commands

| Task | Command |
|------|---------|
| Start locally | `python run.py` |
| Start with Docker | `docker compose up -d` |
| Run DB migrations | `alembic upgrade head` |
| Lint | `ruff check .` |
| Lint (fix) | `ruff check --fix .` |
| Format | `ruff format .` |
| Format (check) | `ruff format --check .` |
| Type check | _(not configured yet)_ |
| Tests | _(tests/ is empty — add pytest tests here)_ |

## Architecture

### Dependency Layers (strict, top imports bottom only)

```
shared/types.py          # Pure Pydantic models, no imports from other layers
    ↑
shared/config.py         # Pydantic Settings, reads env vars
    ↑
shared/database.py       # SQLAlchemy engine/session setup
shared/models.py         # ORM models (may import types, config)
shared/redis_client.py   # Redis utilities
shared/events.py         # Event bus
shared/logging.py        # Structured logging
shared/notifier.py       # Notification helpers
shared/preflight.py      # Health checks
    ↑
integrations/            # External service clients (Slack, Telegram, Linear)
    ↑
orchestrator/            # Task routing, classification, queue, webhooks
claude_runner/           # Claude Code execution loop, prompts, workspace
    ↑
web/                     # Web UI routes and static assets
    ↑
run.py                   # Entry point — wires everything together
```

### Module Boundaries

| Directory | Owns | Must NOT import from |
|-----------|------|---------------------|
| `shared/` | Config, DB, models, types, events, logging | `orchestrator/`, `claude_runner/`, `integrations/`, `web/` |
| `integrations/` | Slack, Telegram, Linear clients | `orchestrator/`, `claude_runner/`, `web/` |
| `orchestrator/` | Task lifecycle, routing, webhooks, scheduling | `claude_runner/`, `web/` |
| `claude_runner/` | Claude Code execution, prompts, workspaces | `web/` |
| `web/` | HTTP UI, static assets | _(can import from all lower layers)_ |
| `migrations/` | Alembic migrations | Everything except `shared/models`, `shared/database` |

### Key Design Patterns

- **Async everywhere**: All I/O uses `async/await` with `asyncio`
- **SQLAlchemy async sessions**: Use `async_session()` context manager from `shared/database.py`
- **Pydantic for validation**: All API inputs/outputs and inter-service data use Pydantic models in `shared/types.py`
- **Structured logging**: Use `structlog` via `shared/logging.py`, never bare `print()`
- **State machine**: Task status transitions go through `orchestrator/state_machine.py`
- **Redis for events**: Real-time updates flow through Redis pub/sub via `shared/events.py`

## Code Style

- **Naming**: snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE for constants
- **Imports**: stdlib → third-party → local, sorted alphabetically within groups
- **Error handling**: Let exceptions propagate unless there's a specific recovery action; use `structlog` to log errors with context
- **No bare `print()`**: Use `structlog.get_logger()` for all output
- **Pydantic models**: Define in `shared/types.py` for cross-module data; local models OK for single-module use
- **SQL models**: All ORM models live in `shared/models.py`
- **Async functions**: Prefix with `async` — never use synchronous DB or HTTP calls in async contexts

## File Organization

- New integrations → `integrations/<service>/main.py`
- New API endpoints → `orchestrator/router.py`
- New webhook handlers → `orchestrator/webhooks/<source>.py`
- New Pydantic types → `shared/types.py`
- New ORM models → `shared/models.py`
- Database migrations → `migrations/versions/` (use `alembic revision --autogenerate -m "description"`)
- Static web assets → `web/static/`
- Max file size guideline: ~500 lines. If a module exceeds this, consider splitting by concern.
