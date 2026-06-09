"""VM-global exclusive lease for the auto-heal loop.

A single Redis key (``HEALTH_LEASE_KEY``) is the mutex: while it is set,
the orchestrator dispatcher (``orchestrator.queue``) refuses to start any
task that is not the health loop's own. The lease is TTL-guarded so a
crashed supervisor can't wedge the VM forever — the holder renews it
periodically while active.

Single-writer by design (the loop is serial), so GET-then-SET/DEL holder
checks are sufficient; no Lua compare-and-set is needed.
"""

from __future__ import annotations

from shared.redis_client import get_redis

HEALTH_LEASE_KEY = "auto-agent:health-loop:lease"


async def acquire_lease(holder: str, *, ttl_seconds: int = 3600) -> bool:
    """Acquire the lease for ``holder``. Returns True iff it was free.

    Uses ``SET key holder NX EX ttl`` — atomic acquire-if-absent.
    """
    r = await get_redis()
    result = await r.set(HEALTH_LEASE_KEY, holder.encode(), nx=True, ex=ttl_seconds)
    return bool(result)


async def lease_holder() -> str | None:
    """Return the current holder string, or None if the lease is free."""
    r = await get_redis()
    value = await r.get(HEALTH_LEASE_KEY)
    return value.decode() if value is not None else None


async def lease_held() -> bool:
    """True iff the lease is currently held by anyone."""
    return await lease_holder() is not None
