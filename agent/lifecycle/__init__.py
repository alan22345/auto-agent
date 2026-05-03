"""Per-phase task lifecycle modules.

Each submodule owns one phase of the task lifecycle (planning, coding, review,
deploy, query, conversation, cleanup) and exposes ``async def handle(event)``
matching the ``EventHandler`` protocol from ``shared.events``. The dispatcher
in ``agent.main`` registers each handler against an event-type pattern on a
shared ``EventBus``.

Cross-cutting helpers live in private modules with leading underscores
(``_orchestrator_api``, ``_agent``, ``_naming``, ``_clarification``).
"""
