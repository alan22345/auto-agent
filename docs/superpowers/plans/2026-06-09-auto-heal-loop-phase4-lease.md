# Auto-Heal Loop — Phase 4 (keystone): VM-global lease + dispatcher gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Checkbox (`- [ ]`) steps.

**Goal:** A VM-global exclusive Redis lease and the dispatcher gate that enforces it — while the lease is held, the orchestrator dispatches ONLY the health loop's own fix tasks; every other task waits. This is the mechanism that prevents the loop from over-saturating the VM.

**Architecture:** A small lease module (`agent/health_loop/lease.py`) over the existing `shared/redis_client.py` (`SET key val NX EX ttl`). The gate is two added checks in `orchestrator/queue.py` (`can_start_task`, `next_eligible_task`). Single-writer (serial loop) so GET-then-SET/DEL holder checks are sufficient — no Lua CAS needed.

**Tech Stack:** Python 3.12, async, redis (`shared.redis_client.get_redis`), SQLAlchemy, pytest.

**Spec:** `docs/superpowers/specs/2026-06-09-auto-heal-loop-design.md` (component 7). Map: this plan's hooks were grounded in `orchestrator/queue.py:73,87` and `shared/redis_client.py::get_redis`.

---

## File structure

- **Create:** `agent/health_loop/lease.py` — `HEALTH_LEASE_KEY`, `acquire_lease`, `renew_lease`, `release_lease`, `lease_holder`, `lease_held`.
- **Create:** `tests/test_health_loop_lease.py` — lease tests with an in-process fake redis.
- **Modify:** `orchestrator/queue.py` — add `is_health_loop_task` + lease checks in `can_start_task` and `next_eligible_task`.
- **Modify/Create:** `tests/test_queue_health_lease.py` — gate tests.

### Reference: redis client (`shared/redis_client.py`)

```python
async def get_redis() -> aioredis.Redis: ...   # decode_responses=False → values are bytes
# usage: r = await get_redis(); await r.set(key, val, nx=True, ex=ttl); await r.get(key); await r.delete(key)
```

---

### Task 1: lease module — acquire / held / holder

**Files:** Create `agent/health_loop/lease.py`, `tests/test_health_loop_lease.py`.

- [ ] **Step 1: Write the failing test (with an in-process fake redis)**

Create `tests/test_health_loop_lease.py`:

```python
"""Phase 4 — VM-global exclusive lease (fake-redis unit tests)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.health_loop import lease


class FakeRedis:
    """Minimal async redis stand-in supporting set(nx,ex)/get/delete."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value if isinstance(value, bytes) else str(value).encode()
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        return 1


@pytest.fixture
def fake_redis():
    fr = FakeRedis()
    with patch.object(lease, "get_redis", AsyncMock(return_value=fr)):
        yield fr


@pytest.mark.asyncio
async def test_acquire_succeeds_when_free_then_blocks_others(fake_redis):
    assert await lease.acquire_lease("supervisor-1", ttl_seconds=60) is True
    # A different holder cannot acquire while held.
    assert await lease.acquire_lease("supervisor-2", ttl_seconds=60) is False


@pytest.mark.asyncio
async def test_lease_held_and_holder_reflect_state(fake_redis):
    assert await lease.lease_held() is False
    assert await lease.lease_holder() is None
    await lease.acquire_lease("supervisor-1", ttl_seconds=60)
    assert await lease.lease_held() is True
    assert await lease.lease_holder() == "supervisor-1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_lease.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.health_loop.lease'`

- [ ] **Step 3: Implement the acquire/held/holder functions**

Create `agent/health_loop/lease.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_lease.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/lease.py tests/test_health_loop_lease.py
git commit -m "feat(health-loop): VM-global lease acquire/held/holder"
```

---

### Task 2: lease module — renew / release (holder-guarded)

**Files:** Modify `agent/health_loop/lease.py`, `tests/test_health_loop_lease.py`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_loop_lease.py`:

```python
@pytest.mark.asyncio
async def test_renew_only_by_holder(fake_redis):
    await lease.acquire_lease("supervisor-1", ttl_seconds=60)
    assert await lease.renew_lease("supervisor-1", ttl_seconds=60) is True
    # A non-holder cannot renew.
    assert await lease.renew_lease("intruder", ttl_seconds=60) is False
    # Still held by the original holder.
    assert await lease.lease_holder() == "supervisor-1"


@pytest.mark.asyncio
async def test_release_only_by_holder(fake_redis):
    await lease.acquire_lease("supervisor-1", ttl_seconds=60)
    # A non-holder cannot release.
    assert await lease.release_lease("intruder") is False
    assert await lease.lease_held() is True
    # The holder can release; afterwards the lease is free.
    assert await lease.release_lease("supervisor-1") is True
    assert await lease.lease_held() is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_lease.py -k "renew or release" -q`
Expected: FAIL — `AttributeError: module 'agent.health_loop.lease' has no attribute 'renew_lease'`

- [ ] **Step 3: Implement renew + release**

Add to `agent/health_loop/lease.py`:

```python
async def renew_lease(holder: str, *, ttl_seconds: int = 3600) -> bool:
    """Extend the TTL — only if ``holder`` currently holds the lease.

    Returns True on renewal, False if the lease is free or held by someone
    else (the supervisor should treat that as 'lost the lease' and stop).
    """
    if await lease_holder() != holder:
        return False
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_lease.py -k "renew or release" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/lease.py tests/test_health_loop_lease.py
git commit -m "feat(health-loop): holder-guarded lease renew/release"
```

---

### Task 3: dispatcher gate in `orchestrator/queue.py`

**Files:** Modify `orchestrator/queue.py`, Create `tests/test_queue_health_lease.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_queue_health_lease.py`:

```python
"""The dispatcher gate: while the health lease is held, only the loop's own
fix tasks may start; everything else is blocked."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator import queue


def _task(*, source_id="", repo_id=None, org_id=1):
    return SimpleNamespace(
        id=1, source_id=source_id, repo_id=repo_id, organization_id=org_id
    )


def test_is_health_loop_task_recognizes_fix_tasks():
    assert queue.is_health_loop_task(_task(source_id="health:42:batch:abc")) is True
    assert queue.is_health_loop_task(_task(source_id="health-loop")) is True
    assert queue.is_health_loop_task(_task(source_id="slack:123")) is False
    assert queue.is_health_loop_task(_task(source_id="")) is False


@pytest.mark.asyncio
async def test_can_start_blocks_non_health_task_when_lease_held():
    task = _task(source_id="slack:123")
    with (
        patch.object(queue, "count_active", AsyncMock(return_value=0)),
        patch.object(queue, "_org_at_concurrency_cap", AsyncMock(return_value=False)),
        patch.object(queue, "_repo_has_active_task", AsyncMock(return_value=False)),
        patch.object(queue, "lease_held", AsyncMock(return_value=True)),
    ):
        assert await queue.can_start_task(session=None, task=task) is False


@pytest.mark.asyncio
async def test_can_start_allows_health_fix_task_when_lease_held():
    task = _task(source_id="health:42:batch:abc")
    with (
        patch.object(queue, "count_active", AsyncMock(return_value=0)),
        patch.object(queue, "_org_at_concurrency_cap", AsyncMock(return_value=False)),
        patch.object(queue, "_repo_has_active_task", AsyncMock(return_value=False)),
        patch.object(queue, "lease_held", AsyncMock(return_value=True)),
    ):
        assert await queue.can_start_task(session=None, task=task) is True


@pytest.mark.asyncio
async def test_can_start_unaffected_when_lease_free():
    task = _task(source_id="slack:123")
    with (
        patch.object(queue, "count_active", AsyncMock(return_value=0)),
        patch.object(queue, "_org_at_concurrency_cap", AsyncMock(return_value=False)),
        patch.object(queue, "_repo_has_active_task", AsyncMock(return_value=False)),
        patch.object(queue, "lease_held", AsyncMock(return_value=False)),
    ):
        assert await queue.can_start_task(session=None, task=task) is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_queue_health_lease.py -q`
Expected: FAIL — `AttributeError: module 'orchestrator.queue' has no attribute 'is_health_loop_task'`

- [ ] **Step 3: Implement the gate**

In `orchestrator/queue.py`:

(a) Add the import near the top (after the existing `from shared.models import ...`):

```python
from agent.health_loop.lease import lease_held
```

(b) Add the helper after `ACTIVE_STATUSES`:

```python
def is_health_loop_task(task: Task) -> bool:
    """True for the auto-heal loop's own tasks (the supervisor and its
    per-batch fix tasks), which are exempt from the lease gate — the lease
    blocks *other* work, never the loop's own."""
    sid = task.source_id or ""
    return sid == "health-loop" or sid.startswith("health:")
```

(c) In `can_start_task`, add the lease gate right after the global-cap check:

```python
async def can_start_task(session: AsyncSession, task: Task) -> bool:
    """Can this specific task start right now?"""
    if await count_active(session) >= settings.max_concurrent_workers:
        return False
    # Health-loop lease: while held, only the loop's own tasks may start.
    if not is_health_loop_task(task) and await lease_held():
        return False
    if task.organization_id is not None and await _org_at_concurrency_cap(
        session, task.organization_id
    ):
        return False
    return not (
        task.repo_id is not None
        and await _repo_has_active_task(session, task.repo_id)
    )
```

(d) In `next_eligible_task`, compute the lease once and skip non-loop tasks while held. Add after the `count_active` early-return (line ~96):

```python
    lease_is_held = await lease_held()
```

and inside the `for t in queued_q.scalars():` loop, as the FIRST check in the body:

```python
        if lease_is_held and not is_health_loop_task(t):
            continue
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_queue_health_lease.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the existing queue tests + the new ones + lint**

Run: `.venv/bin/python3 -m pytest tests/ -k "queue" -q`
Expected: PASS (no regressions in existing queue tests)
Run: `.venv/bin/ruff check orchestrator/queue.py agent/health_loop/lease.py tests/test_health_loop_lease.py tests/test_queue_health_lease.py`
Expected: `All checks passed!`
Run: `.venv/bin/ruff format --check agent/health_loop/lease.py tests/test_health_loop_lease.py tests/test_queue_health_lease.py`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add orchestrator/queue.py tests/test_queue_health_lease.py
git commit -m "feat(orchestrator): health-loop lease gate blocks other dispatch"
```

---

### Phase 4 (keystone) exit criteria

- `agent/health_loop/lease.py`: acquire (NX), holder-guarded renew/release, `lease_held`/`lease_holder`; unit-tested with a fake redis.
- `orchestrator/queue.py`: while the lease is held, `can_start_task` and
  `next_eligible_task` block every non-`health:` task and allow the loop's own;
  unaffected when the lease is free. Existing queue tests still green.

### Remaining Phase 4 integration (needs the VM/live infra to verify — separate effort)
- The supervisor background loop (like `run_po_analysis_loop`): acquire lease →
  on each `repo.graph_ready` (or idle tick) select a batch → file a `health:` fix
  task → await terminal → merge/park → renew lease → idle; Stop/Resume.
- `HealthLoopConfig` model + alembic migration (`enabled`, `cleanup_branch`,
  `batch_size`, `suppressed_finding_hashes`, `state`).
```
