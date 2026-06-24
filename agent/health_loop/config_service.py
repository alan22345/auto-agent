"""Async CRUD for HealthLoopConfig (per-repo auto-heal loop settings).

DB access from the agent layer is allowed (agent → shared), mirroring
agent/po_analyzer.py. All writes commit within an ``async_session`` block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from shared.database import async_session
from shared.models import HealthLoopConfig

if TYPE_CHECKING:
    from collections.abc import Callable


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


async def list_active_configs() -> list[HealthLoopConfig]:
    """Return every enabled config (any state). The supervisor scans these to
    decide which repo's loop should hold the VM-global lease."""
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.enabled == True)  # noqa: E712
        )
        return list(result.scalars().all())


async def _apply(repo_id: int, mutate: Callable[[HealthLoopConfig], None]) -> None:
    """Load the config for ``repo_id``, apply ``mutate``, and commit.

    A no-op when the repo has no config row — every mutator is a safe no-op on
    an unconfigured repo rather than raising.
    """
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is not None:
            mutate(cfg)
            await session.commit()


async def set_enabled(repo_id: int, enabled: bool) -> None:
    await _apply(repo_id, lambda cfg: setattr(cfg, "enabled", enabled))


async def set_state(repo_id: int, state: str) -> None:
    """Set the supervisor lifecycle state (idle|running|paused)."""
    await _apply(repo_id, lambda cfg: setattr(cfg, "state", state))


async def set_started_by(repo_id: int, user_id: int | None) -> None:
    """Record who enabled the loop — the coder borrows their credentials."""
    await _apply(repo_id, lambda cfg: setattr(cfg, "started_by_user_id", user_id))


async def update_settings(
    repo_id: int, *, batch_size: int | None = None, cleanup_branch: str | None = None
) -> None:
    """Update user-tunable settings; only the provided fields change."""

    def mutate(cfg: HealthLoopConfig) -> None:
        if batch_size is not None:
            cfg.batch_size = batch_size
        if cleanup_branch is not None:
            cfg.cleanup_branch = cleanup_branch

    await _apply(repo_id, mutate)


async def suppress_finding(repo_id: int, finding_hash: str) -> None:
    """Add ``finding_hash`` to the won't-fix list (idempotent)."""
    await _apply(
        repo_id,
        lambda cfg: setattr(
            cfg,
            "suppressed_finding_hashes",
            _dedup_append(list(cfg.suppressed_finding_hashes or []), finding_hash),
        ),
    )


async def get_suppressed(repo_id: int) -> set[str]:
    """Return the suppressed finding-hash set (empty if unconfigured)."""
    cfg = await get_config(repo_id)
    return set(cfg.suppressed_finding_hashes or []) if cfg else set()


async def get_addressed(repo_id: int) -> set[str]:
    """Return the addressed (merged-or-parked) finding-hash set."""
    cfg = await get_config(repo_id)
    return set(cfg.addressed_finding_hashes or []) if cfg else set()


async def mark_addressed(repo_id: int, finding_hashes: list[str]) -> None:
    """Record ``finding_hashes`` as acted-on so they're never re-picked."""

    def mutate(cfg: HealthLoopConfig) -> None:
        addressed = list(cfg.addressed_finding_hashes or [])
        for h in finding_hashes:
            addressed = _dedup_append(addressed, h)
        cfg.addressed_finding_hashes = addressed

    await _apply(repo_id, mutate)


async def set_current_batch(repo_id: int, batch: list[dict]) -> None:
    """Set the in-flight batch shown in the status strip ([] when idle)."""
    await _apply(repo_id, lambda cfg: setattr(cfg, "current_batch", batch))


async def record_outcome(
    repo_id: int, *, merged: int = 0, parked: int = 0, touch_run: bool = True
) -> None:
    """Bump the merged/parked tallies and (optionally) ``last_run_at``."""

    def mutate(cfg: HealthLoopConfig) -> None:
        cfg.merged_count = (cfg.merged_count or 0) + merged
        cfg.parked_count = (cfg.parked_count or 0) + parked
        if touch_run:
            cfg.last_run_at = datetime.now(UTC)

    await _apply(repo_id, mutate)


async def set_cleanup_pr_url(repo_id: int, url: str) -> None:
    """Store the standing cleanup → main PR link for the status strip."""
    await _apply(repo_id, lambda cfg: setattr(cfg, "cleanup_pr_url", url))
