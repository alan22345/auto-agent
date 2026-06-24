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


async def renew_lease(holder: str, *, ttl_seconds: int = 3600) -> bool:
    """Extend the TTL — only if ``holder`` currently holds the lease.

    Returns True on renewal, False if the lease is free or held by someone
    else (the supervisor should treat that as 'lost the lease' and stop).
    """
    if await lease_holder() != holder:
        return False
    # TOCTOU: this GET-then-SET is non-atomic and uses SET without NX, so if the
    # TTL expires in the gap it could re-create/clobber a lease. Safe under the
    # serial single-supervisor design; the follow-up if the loop ever becomes
    # concurrent is a Lua compare-and-set (GET + PEXPIRE atomically).
    r = await get_redis()
    await r.set(HEALTH_LEASE_KEY, holder.encode(), ex=ttl_seconds)
    return True


async def release_lease(holder: str) -> bool:
    """Release the lease — only if ``holder`` currently holds it.

    Returns True if released, False if it was free or held by someone else
    (never steal another holder's lease).
    """
    if await lease_holder() != holder:
        return False
    r = await get_redis()
    await r.delete(HEALTH_LEASE_KEY)
    return True
