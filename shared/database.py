from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.config import settings

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Shared team-memory DB (atlas). Bound lazily via a separate engine so the
# orchestrator's operational DB stays untouched.
team_memory_engine = (
    create_async_engine(settings.team_memory_database_url, echo=False)
    if settings.team_memory_database_url
    else None
)
team_memory_session = (
    async_sessionmaker(team_memory_engine, class_=AsyncSession, expire_on_commit=False)
    if team_memory_engine is not None
    else None
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
