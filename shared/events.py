"""Flexible event system for inter-service communication via Redis Streams.

Three seams live in this module:

  * **Taxonomy** — ``TaskEventType``, ``POEventType``, ``ArchitectureEventType``,
    ``RepoEventType``, ``HumanEventType`` (``StrEnum``s) plus a factory
    function for every event in the system. Producers should use the
    factories (``task_created(task.id)``); the enums are the closed set of
    event types the system can emit. Add a member here when adding a new
    event — that's the *only* place the wire string lives.

  * **Dispatcher** — ``Event`` + ``EventBus`` are the in-process consumer
    seam. ``EventBus.on(pattern, handler)`` registers async handlers
    against glob patterns over ``event.type``; ``EventBus.dispatch(event)``
    routes to every match. Patterns stay strings (e.g. ``"task.*"``) so
    glob matching is uniform across enum + free-form input.

  * **Publisher** — ``Publisher`` Protocol + ``publish()``: the
    cross-process publish seam (ADR-007). ``RedisStreamPublisher`` is the
    production adapter; ``InMemoryPublisher`` is the test adapter.
    Callers never see Redis.

``Event.type`` is intentionally typed ``str``, not the enum union: the
``EventBus`` matches glob patterns over it, and a downstream consumer
reading the stream may legitimately receive an event type added after it
was last deployed. ``StrEnum`` is a ``str`` subclass, so assigning an
enum member to ``Event.type`` works without ``.value`` and serialises to
the same wire bytes — wire format is byte-identical to the legacy
``type="task.created"`` string literal.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Stream key — the single Redis Streams key all events are written to and
# read from. Lives here (not in shared/redis_client) so the consumer-side
# helpers and the publisher both import from one source of truth.
# ---------------------------------------------------------------------------

STREAM_KEY = "autoagent:events"


# ---------------------------------------------------------------------------
# Taxonomy — the closed set of event types the system can emit.
#
# Wire strings are listed inside the enum; factory functions below assemble
# Event instances with the right payload shape per type. Producer call
# sites should always go through a factory, never construct Event() with a
# bare type=... string.
# ---------------------------------------------------------------------------


class TaskEventType(StrEnum):
    CREATED = "task.created"
    CLASSIFIED = "task.classified"
    STATUS_CHANGED = "task.status_changed"
    QUERY = "task.query"
    START_PLANNING = "task.start_planning"
    START_CODING = "task.start_coding"
    PLAN_READY = "task.plan_ready"
    DEPLOY_PREVIEW = "task.deploy_preview"
    CI_PASSED = "task.ci_passed"
    CI_FAILED = "task.ci_failed"
    REVIEW_COMPLETE = "task.review_complete"
    REVIEW_APPROVED = "task.review_approved"
    REVIEW_COMMENTS_ADDRESSED = "task.review_comments_addressed"
    APPROVED = "task.approved"
    REJECTED = "task.rejected"
    CLARIFICATION_NEEDED = "task.clarification_needed"
    CLARIFICATION_RESPONSE = "task.clarification_response"
    CLARIFICATION_RESOLVED = "task.clarification_resolved"
    BLOCKED = "task.blocked"
    FAILED = "task.failed"
    CLEANUP = "task.cleanup"
    DEV_DEPLOYED = "task.dev_deployed"
    DEV_DEPLOY_FAILED = "task.dev_deploy_failed"
    FEEDBACK = "task.feedback"
    DONE = "task.done"
    START_QUEUED = "task.start_queued"
    SUBTASK_PROGRESS = "task.subtask_progress"
    LGTM_RECEIVED = "task.lgtm_received"
    MERGE_CONFLICT_DETECTED = "task.merge_conflict_detected"
    MERGE_CONFLICT_RESOLVED = "task.merge_conflict_resolved"
    MERGE_CONFLICT_RESOLUTION_FAILED = "task.merge_conflict_resolution_failed"
    VERIFY_STARTED = "task.verify_started"
    VERIFY_PASSED = "task.verify_passed"
    VERIFY_FAILED = "task.verify_failed"
    VERIFY_SKIPPED_NO_RUNNER = "task.verify_skipped_no_runner"
    CODING_SERVER_BOOT_FAILED = "task.coding_server_boot_failed"
    REVIEW_UI_CHECK_STARTED = "task.review_ui_check_started"
    REVIEW_SKIPPED_NO_RUNNER = "task.review_skipped_no_runner"


class POEventType(StrEnum):
    ANALYZE = "po.analyze"
    ANALYSIS_QUEUED = "po.analysis_queued"
    ANALYSIS_STARTED = "po.analysis_started"
    ANALYSIS_FAILED = "po.analysis_failed"
    SUGGESTIONS_READY = "po.suggestions_ready"
    MARKET_RESEARCH_STARTED = "po.market_research_started"
    MARKET_RESEARCH_COMPLETED = "po.market_research_completed"
    MARKET_RESEARCH_FAILED = "po.market_research_failed"


class ArchitectureEventType(StrEnum):
    ANALYSIS_STARTED = "architecture.analysis_started"
    ANALYSIS_FAILED = "architecture.analysis_failed"
    SUGGESTIONS_READY = "architecture.suggestions_ready"


class RepoEventType(StrEnum):
    ONBOARD = "repo.onboard"
    DELETED = "repo.deleted"
    GRAPH_REQUESTED = "repo.graph_requested"
    GRAPH_READY = "repo.graph_ready"
    GRAPH_FAILED = "repo.graph_failed"


class HumanEventType(StrEnum):
    MESSAGE = "human.message"


class Event(BaseModel):
    """A single event on the bus.

    Attributes:
        type:     Event type string. Producers should pass a member of one
                  of the ``*EventType`` enums via a factory; the field is
                  typed ``str`` because ``EventBus`` matches glob patterns
                  against it and consumers must accept unknown strings
                  read off the wire.
        task_id:  Optional task this event relates to. ``0`` for events
                  that are repo- or system-scoped (PO analysis, repo
                  onboarding) and have no task.
        payload:  Arbitrary data — consumers should validate what they need.
        timestamp: When the event was created (UTC).
    """

    type: str
    task_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # --- Redis serialisation helpers ---

    def to_redis(self) -> dict[str, str]:
        return {
            "type": str(self.type),
            "data": json.dumps(self.model_dump(mode="json")),
        }

    @classmethod
    def from_redis(cls, data: dict[bytes | str, bytes | str]) -> Event:
        raw = data.get(b"data") or data.get("data")
        if isinstance(raw, bytes):
            raw = raw.decode()
        return cls.model_validate_json(raw)


# ---------------------------------------------------------------------------
# Factory constructors — one per event type.
#
# Trivial factories (task_id only) keep producer call sites uniform; richer
# factories encode the payload schema per event so a typo in a payload key
# becomes a TypeError at the producer call site instead of a silent miss
# downstream.
# ---------------------------------------------------------------------------


def task_created(task_id: int) -> Event:
    return Event(type=TaskEventType.CREATED, task_id=task_id)


def task_classified(
    task_id: int, complexity: str, reasoning: str = "", **extra: Any
) -> Event:
    return Event(
        type=TaskEventType.CLASSIFIED,
        task_id=task_id,
        payload={"complexity": complexity, "reasoning": reasoning, **extra},
    )


def task_status_changed(task_id: int, status: str, message: str = "") -> Event:
    return Event(
        type=TaskEventType.STATUS_CHANGED,
        task_id=task_id,
        payload={"status": status, "message": message},
    )


def task_query(task_id: int) -> Event:
    return Event(type=TaskEventType.QUERY, task_id=task_id)


def task_start_planning(task_id: int, feedback: str | None = None) -> Event:
    payload: dict[str, Any] = {}
    if feedback is not None:
        payload["feedback"] = feedback
    return Event(type=TaskEventType.START_PLANNING, task_id=task_id, payload=payload)


def task_start_coding(task_id: int, retry_reason: str | None = None) -> Event:
    payload: dict[str, Any] = {}
    if retry_reason is not None:
        payload["retry_reason"] = retry_reason
    return Event(type=TaskEventType.START_CODING, task_id=task_id, payload=payload)


def task_plan_ready(task_id: int, plan: str) -> Event:
    return Event(type=TaskEventType.PLAN_READY, task_id=task_id, payload={"plan": plan})


def task_deploy_preview(task_id: int) -> Event:
    return Event(type=TaskEventType.DEPLOY_PREVIEW, task_id=task_id)


def task_ci_passed(task_id: int) -> Event:
    return Event(type=TaskEventType.CI_PASSED, task_id=task_id)


def task_ci_failed(task_id: int, reason: str) -> Event:
    return Event(type=TaskEventType.CI_FAILED, task_id=task_id, payload={"reason": reason})


def task_review_complete(
    task_id: int,
    *,
    review: str,
    pr_url: str,
    approved: bool,
    branch: str = "",
    fixes: str = "",
) -> Event:
    payload: dict[str, Any] = {
        "review": review,
        "pr_url": pr_url,
        "approved": approved,
    }
    if branch:
        payload["branch"] = branch
    if fixes:
        payload["fixes"] = fixes
    return Event(type=TaskEventType.REVIEW_COMPLETE, task_id=task_id, payload=payload)


def task_review_approved(task_id: int) -> Event:
    return Event(type=TaskEventType.REVIEW_APPROVED, task_id=task_id)


def task_review_comments_addressed(task_id: int, *, output: str, pr_url: str) -> Event:
    return Event(
        type=TaskEventType.REVIEW_COMMENTS_ADDRESSED,
        task_id=task_id,
        payload={"output": output, "pr_url": pr_url},
    )


def task_approved(task_id: int) -> Event:
    return Event(type=TaskEventType.APPROVED, task_id=task_id)


def task_rejected(task_id: int, feedback: str) -> Event:
    return Event(type=TaskEventType.REJECTED, task_id=task_id, payload={"feedback": feedback})


def task_clarification_needed(task_id: int, question: str, phase: str = "") -> Event:
    payload: dict[str, Any] = {"question": question}
    if phase:
        payload["phase"] = phase
    return Event(type=TaskEventType.CLARIFICATION_NEEDED, task_id=task_id, payload=payload)


def task_clarification_response(task_id: int, answer: str) -> Event:
    return Event(
        type=TaskEventType.CLARIFICATION_RESPONSE,
        task_id=task_id,
        payload={"answer": answer},
    )


def task_clarification_resolved(task_id: int, output: str = "") -> Event:
    payload: dict[str, Any] = {}
    if output:
        payload["output"] = output
    return Event(type=TaskEventType.CLARIFICATION_RESOLVED, task_id=task_id, payload=payload)


def task_blocked(task_id: int, error: str = "") -> Event:
    payload: dict[str, Any] = {}
    if error:
        payload["error"] = error
    return Event(type=TaskEventType.BLOCKED, task_id=task_id, payload=payload)


def task_failed(task_id: int, error: str = "") -> Event:
    payload: dict[str, Any] = {}
    if error:
        payload["error"] = error
    return Event(type=TaskEventType.FAILED, task_id=task_id, payload=payload)


def task_cleanup(task_id: int) -> Event:
    return Event(type=TaskEventType.CLEANUP, task_id=task_id)


def task_dev_deployed(task_id: int, *, branch: str, output: str = "", pr_url: str = "") -> Event:
    return Event(
        type=TaskEventType.DEV_DEPLOYED,
        task_id=task_id,
        payload={"branch": branch, "output": output, "pr_url": pr_url},
    )


def task_dev_deploy_failed(
    task_id: int, *, branch: str = "", output: str = "", pr_url: str = ""
) -> Event:
    return Event(
        type=TaskEventType.DEV_DEPLOY_FAILED,
        task_id=task_id,
        payload={"branch": branch, "output": output, "pr_url": pr_url},
    )


def task_feedback(task_id: int, *, message_id: int, sender: str) -> Event:
    return Event(
        type=TaskEventType.FEEDBACK,
        task_id=task_id,
        payload={"message_id": message_id, "sender": sender},
    )


def task_done(task_id: int) -> Event:
    return Event(type=TaskEventType.DONE, task_id=task_id)


def task_start_queued(task_id: int) -> Event:
    return Event(type=TaskEventType.START_QUEUED, task_id=task_id)


def task_subtask_progress(
    task_id: int, *, current: int, total: int, title: str, status: str
) -> Event:
    return Event(
        type=TaskEventType.SUBTASK_PROGRESS,
        task_id=task_id,
        payload={
            "current": current,
            "total": total,
            "title": title,
            "status": status,
        },
    )


def task_lgtm_received(task_id: int, reviewer: str, pr_url: str) -> Event:
    return Event(
        type=TaskEventType.LGTM_RECEIVED,
        task_id=task_id,
        payload={"reviewer": reviewer, "pr_url": pr_url},
    )


def task_merge_conflict_detected(task_id: int, pr_url: str, trigger: str) -> Event:
    return Event(
        type=TaskEventType.MERGE_CONFLICT_DETECTED,
        task_id=task_id,
        payload={"pr_url": pr_url, "trigger": trigger},
    )


def task_merge_conflict_resolved(task_id: int, head_branch: str) -> Event:
    return Event(
        type=TaskEventType.MERGE_CONFLICT_RESOLVED,
        task_id=task_id,
        payload={"head_branch": head_branch},
    )


def task_merge_conflict_resolution_failed(task_id: int, reason: str) -> Event:
    return Event(
        type=TaskEventType.MERGE_CONFLICT_RESOLUTION_FAILED,
        task_id=task_id,
        payload={"reason": reason},
    )


def po_analyze(repo_id: int, repo_name: str) -> Event:
    return Event(
        type=POEventType.ANALYZE,
        task_id=0,
        payload={"repo_id": repo_id, "repo_name": repo_name},
    )


def po_analysis_queued(repo_name: str, position: int) -> Event:
    return Event(
        type=POEventType.ANALYSIS_QUEUED,
        task_id=0,
        payload={"repo_name": repo_name, "position": position},
    )


def po_analysis_started(repo_name: str) -> Event:
    return Event(
        type=POEventType.ANALYSIS_STARTED,
        task_id=0,
        payload={"repo_name": repo_name},
    )


def po_analysis_failed(repo_name: str, reason: str = "") -> Event:
    payload: dict[str, Any] = {"repo_name": repo_name}
    if reason:
        payload["reason"] = reason
    return Event(type=POEventType.ANALYSIS_FAILED, task_id=0, payload=payload)


def po_suggestions_ready(repo_name: str, count: int) -> Event:
    return Event(
        type=POEventType.SUGGESTIONS_READY,
        task_id=0,
        payload={"repo_name": repo_name, "count": count},
    )


def market_research_started(repo_name: str) -> Event:
    return Event(
        type=POEventType.MARKET_RESEARCH_STARTED,
        task_id=0,
        payload={"repo_name": repo_name},
    )


def market_research_completed(
    repo_name: str,
    brief_id: int,
    n_competitors: int,
    n_findings: int,
    partial: bool,
) -> Event:
    return Event(
        type=POEventType.MARKET_RESEARCH_COMPLETED,
        task_id=0,
        payload={
            "repo_name": repo_name,
            "brief_id": brief_id,
            "n_competitors": n_competitors,
            "n_findings": n_findings,
            "partial": partial,
        },
    )


def market_research_failed(repo_name: str, reason: str = "") -> Event:
    payload: dict[str, Any] = {"repo_name": repo_name}
    if reason:
        payload["reason"] = reason
    return Event(
        type=POEventType.MARKET_RESEARCH_FAILED, task_id=0, payload=payload,
    )


def architecture_analysis_started(repo_name: str) -> Event:
    return Event(
        type=ArchitectureEventType.ANALYSIS_STARTED,
        task_id=0,
        payload={"repo_name": repo_name},
    )


def architecture_analysis_failed(repo_name: str, reason: str = "") -> Event:
    payload: dict[str, Any] = {"repo_name": repo_name}
    if reason:
        payload["reason"] = reason
    return Event(type=ArchitectureEventType.ANALYSIS_FAILED, task_id=0, payload=payload)


def architecture_suggestions_ready(repo_name: str, count: int) -> Event:
    return Event(
        type=ArchitectureEventType.SUGGESTIONS_READY,
        task_id=0,
        payload={"repo_name": repo_name, "count": count},
    )


def repo_onboard(repo_id: int, repo_name: str) -> Event:
    return Event(
        type=RepoEventType.ONBOARD,
        task_id=0,
        payload={"repo_id": repo_id, "repo_name": repo_name},
    )


def repo_deleted(repo_name: str) -> Event:
    return Event(type=RepoEventType.DELETED, task_id=0, payload={"repo_name": repo_name})


def repo_graph_requested(*, repo_id: int, request_id: str) -> Event:
    """ADR-016 §10 — the refresh endpoint publishes this; the analyser
    handler consumes it. ``request_id`` is a UUID the caller can correlate
    with the eventual READY/FAILED event."""
    return Event(
        type=RepoEventType.GRAPH_REQUESTED,
        task_id=0,
        payload={"repo_id": repo_id, "request_id": request_id},
    )


def repo_graph_ready(
    *, repo_id: int, repo_graph_id: int, commit_sha: str, status: str,
) -> Event:
    """Published by the analyser after a row is written. ``status`` is one
    of ``"ok"``, ``"partial"`` per ADR-016 §10."""
    return Event(
        type=RepoEventType.GRAPH_READY,
        task_id=0,
        payload={
            "repo_id": repo_id,
            "repo_graph_id": repo_graph_id,
            "commit_sha": commit_sha,
            "status": status,
        },
    )


def repo_graph_failed(*, repo_id: int, error: str) -> Event:
    """Published when the whole analysis fails (clone failure, lock
    contention, DB write). Per-area parser failures are surfaced as
    ``AreaStatus.failed`` inside a ``REPO_GRAPH_READY`` blob with
    ``status="partial"`` — they are *not* published as failures."""
    return Event(
        type=RepoEventType.GRAPH_FAILED,
        task_id=0,
        payload={"repo_id": repo_id, "error": error},
    )


def human_message(task_id: int, message: str, source: str) -> Event:
    return Event(
        type=HumanEventType.MESSAGE,
        task_id=task_id,
        payload={"message": message, "source": source},
    )


def verify_started(task_id: int, cycle: int) -> Event:
    return Event(
        type=TaskEventType.VERIFY_STARTED,
        task_id=task_id,
        payload={"cycle": cycle},
    )


def verify_passed(task_id: int, cycle: int) -> Event:
    return Event(
        type=TaskEventType.VERIFY_PASSED,
        task_id=task_id,
        payload={"cycle": cycle},
    )


def verify_failed(task_id: int, cycle: int, reason: str) -> Event:
    return Event(
        type=TaskEventType.VERIFY_FAILED,
        task_id=task_id,
        payload={"cycle": cycle, "reason": reason},
    )


def verify_skipped_no_runner(task_id: int) -> Event:
    return Event(type=TaskEventType.VERIFY_SKIPPED_NO_RUNNER, task_id=task_id, payload={})


def coding_server_boot_failed(task_id: int, reason: str) -> Event:
    return Event(
        type=TaskEventType.CODING_SERVER_BOOT_FAILED,
        task_id=task_id,
        payload={"reason": reason},
    )


def review_ui_check_started(task_id: int, cycle: int) -> Event:
    return Event(
        type=TaskEventType.REVIEW_UI_CHECK_STARTED,
        task_id=task_id,
        payload={"cycle": cycle},
    )


def review_skipped_no_runner(task_id: int) -> Event:
    return Event(type=TaskEventType.REVIEW_SKIPPED_NO_RUNNER, task_id=task_id, payload={})


# ---------------------------------------------------------------------------
# Handler registry
#
# Services register async handler functions against *glob-style* event
# patterns.  The EventBus dispatches incoming events to every matching
# handler.
#
# Patterns:
#   "task.created"      — exact match
#   "task.*"            — matches task.created, task.classified, …
#   "*"                 — matches everything
# ---------------------------------------------------------------------------

EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """In-process event dispatcher with glob pattern matching."""

    def __init__(self) -> None:
        self._handlers: list[tuple[str, EventHandler]] = []

    def on(self, pattern: str, handler: EventHandler) -> None:
        """Register *handler* for events whose type matches *pattern*."""
        self._handlers.append((pattern, handler))

    async def dispatch(self, event: Event) -> None:
        """Dispatch *event* to all handlers whose pattern matches."""
        for pattern, handler in self._handlers:
            if fnmatch.fnmatch(event.type, pattern):
                await handler(event)


# ---------------------------------------------------------------------------
# Publisher seam
#
# Production code calls ``await publish(event)`` — a single line. The
# module-level helper delegates to whichever Publisher was registered at
# startup. Tests swap in ``InMemoryPublisher`` via ``set_publisher`` so they
# can assert against captured events without touching Redis.
# ---------------------------------------------------------------------------


class Publisher(Protocol):
    """Anything that can accept an Event for downstream consumers."""

    async def publish(self, event: Event) -> None: ...

    async def aclose(self) -> None: ...


class RedisStreamPublisher:
    """Production adapter — owns one long-lived ``redis.asyncio.Redis`` client.

    The previous pattern opened and closed a TCP connection per publish, which
    is what every caller's ``r = await get_redis() / await r.aclose()`` dance
    was working around. ``redis.asyncio.Redis`` already pools connections
    internally, so a single lazy-instantiated client suffices for the lifetime
    of the process.
    """

    def __init__(self, url: str, stream_key: str = STREAM_KEY) -> None:
        self._url = url
        self._stream_key = stream_key
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    import redis.asyncio as aioredis

                    self._client = aioredis.from_url(self._url, decode_responses=False)
        return self._client

    async def publish(self, event: Event) -> None:
        client = await self._get_client()
        await client.xadd(self._stream_key, event.to_redis())

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class InMemoryPublisher:
    """Test adapter — captures published events for assertion.

    Use ``events`` to inspect every event published since construction. Use
    ``wait_for(type)`` to await an event of a given type when the publish
    happens in another task.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._waiters: list[tuple[str, asyncio.Future[Event]]] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)
        # Resolve any matching waiters
        remaining: list[tuple[str, asyncio.Future[Event]]] = []
        for pattern, fut in self._waiters:
            if not fut.done() and fnmatch.fnmatch(event.type, pattern):
                fut.set_result(event)
            else:
                remaining.append((pattern, fut))
        self._waiters = remaining

    async def wait_for(self, event_type: str, timeout: float = 1.0) -> Event:
        """Return the next event matching *event_type* (glob pattern allowed).

        Returns immediately if a matching event was already published.
        """
        for ev in self.events:
            if fnmatch.fnmatch(ev.type, event_type):
                return ev
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Event] = loop.create_future()
        self._waiters.append((event_type, fut))
        return await asyncio.wait_for(fut, timeout=timeout)

    def clear(self) -> None:
        self.events.clear()
        for _, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()

    async def aclose(self) -> None:
        self.clear()


_publisher: Publisher | None = None


def set_publisher(publisher: Publisher) -> None:
    """Register the active publisher. Called once at process start (production)
    and per-test (in fixtures).

    Does NOT aclose the previous publisher — if the caller is replacing a live
    one, it owns the cleanup. Production wires a single RedisStreamPublisher in
    ``run.py``'s lifespan and acloses it on shutdown; tests use a per-test
    fixture that restores the previous reference without calling this again.
    """
    global _publisher
    _publisher = publisher


def get_publisher() -> Publisher:
    """Return the active publisher. Raises if none is registered — every
    process must wire one in before publishing."""
    if _publisher is None:
        raise RuntimeError(
            "No Publisher registered. Call set_publisher(...) at startup, "
            "or install an InMemoryPublisher in a test fixture."
        )
    return _publisher


async def publish(event: Event) -> None:
    """Publish *event* through the active publisher.

    This is the public publish seam — every emitter calls this single
    function. Connection lifecycle is owned by the publisher, not the caller.
    """
    await get_publisher().publish(event)
