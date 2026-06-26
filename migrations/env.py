import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from shared.config import settings
from shared.models import Base

# Stable 64-bit key for the migration advisory lock. Every alembic run (app
# boot self-heal AND the deploy one-off) takes this lock, so two migrators can
# never apply DDL concurrently — they serialize instead of racing into a
# half-applied, rolled-back state.
_MIGRATION_LOCK_KEY = 0x4155544F4147  # "AUTOAG"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the URL from alembic.ini with the one derived from .env
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # Bound how long any single DDL statement waits on a lock held by a
        # still-draining container, so contention fails fast (and the caller's
        # retry can win a clean window) instead of hanging or half-applying.
        connection.execute(text("SET LOCAL lock_timeout = '15s'"))
        # Serialize concurrent migrators (boot self-heal vs deploy one-off).
        # Held until this transaction ends, so the whole upgrade is exclusive.
        connection.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _MIGRATION_LOCK_KEY})
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
