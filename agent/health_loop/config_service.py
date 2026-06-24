"""Async CRUD for HealthLoopConfig (per-repo auto-heal loop settings).

DB access from the agent layer is allowed (agent → shared), mirroring
agent/po_analyzer.py. All writes commit within an ``async_session`` block.
"""

from __future__ import annotations

from sqlalchemy import select

from shared.database import async_session
from shared.models import HealthLoopConfig


def _dedup_append(items: list[str], value: str) -> list[str]:
    """Append ``value`` once, preserving order. Pure."""
    return items if value in items else [*items, value]


async def get_config(repo_id: int) -> HealthLoopConfig | None:
    """Return the loop config for ``repo_id``, or None if not configured."""
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        return result.scalar_one_or_none()


async def get_or_create_config(repo_id: int, organization_id: int) -> HealthLoopConfig:
    """Return the config, creating a default (disabled) row if absent."""
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is None:
            cfg = HealthLoopConfig(repo_id=repo_id, organization_id=organization_id)
            session.add(cfg)
            await session.commit()
            await session.refresh(cfg)
        return cfg


async def set_enabled(repo_id: int, enabled: bool) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is not None:
            cfg.enabled = enabled
            await session.commit()


async def set_state(repo_id: int, state: str) -> None:
    """Set the supervisor lifecycle state (idle|running|paused)."""
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is not None:
            cfg.state = state
            await session.commit()


async def suppress_finding(repo_id: int, finding_hash: str) -> None:
    """Add ``finding_hash`` to the won't-fix list (idempotent)."""
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is not None:
            cfg.suppressed_finding_hashes = _dedup_append(
                list(cfg.suppressed_finding_hashes or []), finding_hash
            )
            await session.commit()


async def get_suppressed(repo_id: int) -> set[str]:
    """Return the suppressed finding-hash set (empty if unconfigured)."""
    cfg = await get_config(repo_id)
    return set(cfg.suppressed_finding_hashes or []) if cfg else set()
