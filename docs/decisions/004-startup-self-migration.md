[ADR-004] Run Alembic Migrations at Lifespan Startup

## Status

Accepted

## Context

Auto-agent ships SQLAlchemy ORM models with matching Alembic migrations under
`migrations/versions/`. The deploy script (`scripts/deploy.sh`) only runs
`alembic upgrade head` when invoked with the explicit `migrate` argument:

```bash
./scripts/deploy.sh          # rsync + docker build + restart, NO migrations
./scripts/deploy.sh migrate  # rsync + alembic upgrade + docker build + restart
```

The lifespan hook in `run.py` calls `Base.metadata.create_all`, which creates
*missing tables* but never *missing columns* on existing tables. So when a
default deploy ships new code that adds a column to an existing table, the
container starts but its first SQL query against that column raises
`UndefinedColumn`. The container fails its `/health` check, the deploy
harness marks the deploy failed, and — because this repo has no GitHub
Actions — the failure has no check-run to attribute it to. The harness
emits the misleading message "Deployment failed. Reason: No failed check
runs found".

This bit task-133's branch when migration `021_intake_qa_and_architecture_mode`
landed: the new code queried `tasks.intake_qa` and `freeform_configs.architecture_mode`
columns that the deployed DB didn't have. Operators had to ssh to the VM and
run `docker compose run alembic upgrade head` manually.

The same trap waits for every future migration the auto-agent itself produces.
Auto-agent runs unattended; it has no operator nearby to remember the migrate
flag.

## Decision

Run `alembic upgrade head` automatically as the first step of the FastAPI
lifespan, BEFORE `Base.metadata.create_all`. New helper
`_run_alembic_upgrade_sync()` in `run.py`:

- Loads `alembic.ini` from the project root.
- Calls `command.upgrade(cfg, "head")`. Idempotent — Alembic skips revisions
  already applied.
- Wrapped in `asyncio.to_thread()` because `migrations/env.py::run_migrations_online`
  uses `asyncio.run(...)`, which can't nest inside an existing event loop.
- Wrapped in `try/except Exception` so a broken migration logs and lets the
  app boot. PostgreSQL applies migrations in a transaction, so a failed
  migration leaves the DB in its previous consistent state — booting with
  the old schema is safe; refusing to boot would block all customer traffic.

Order matters: alembic FIRST, then create_all. Alembic's `alembic_version`
table records which revisions have been applied; if `create_all` ran first
and created a fresh table, alembic would then re-apply migrations against a
schema it didn't bootstrap, which can fail.

## Consequences

- **Positive**: Every deploy — VM, local docker, manual `docker compose up`,
  CI smoke — converges the schema to head before serving traffic. The
  `migrate` flag in `scripts/deploy.sh` is now redundant for normal use; the
  flag is kept for the explicit-control case (e.g. running migrations against
  a non-running container).
- **Positive**: Default-path deploys for the auto-agent harness now self-heal
  after schema changes ship — no human ssh needed.
- **Negative**: Container startup is slower by however long `alembic upgrade
  head` takes (typically <1s when no revisions are pending; up to several
  seconds during a deploy that ships a migration). Acceptable.
- **Negative**: A broken migration is silently swallowed at startup — the
  container boots with the previous schema. Operators must watch logs for
  `alembic upgrade head failed at startup`. The trade-off chosen: a logged
  warning + working old schema beats a process that won't boot at all.
- **Trade-off rejected**: Crash on migration failure. Considered, rejected
  because the failure mode then becomes "all deploys fail until someone
  fixes the migration", which is operationally worse than "new feature
  doesn't work until someone fixes the migration."
- **Trade-off rejected**: Move the alembic call into `scripts/deploy.sh`'s
  default path. Considered, rejected because the auto-agent harness uses
  its own deploy mechanism and can't be assumed to call `scripts/deploy.sh`
  exactly. Putting the migration in lifespan covers all entry points.

## References

- `run.py::_run_alembic_upgrade_sync` and `lifespan`.
- `migrations/env.py::run_migrations_online` — the source of the nested
  asyncio constraint.
- `tests/test_startup_migration.py` — covers the happy path, exception
  swallowing, and the missing-`alembic.ini` path.
- Commit `42beed8` — the fix.
