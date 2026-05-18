"""Scaffold lifecycle — ADR-018.

A SCAFFOLD parent task orchestrates a 5-phase flow:

  A. Intent grill — agent grills the user; writes ``.auto-agent/intent.md``.
  B. Root architect — writes ``.auto-agent/adrs/000-system.md`` listing
     ≤7 domains. Then the user (or PO standin) gates.
  C. Domain architects (serial) — one agent per domain, each writes
     ``.auto-agent/adrs/<n>-<slug>.md``. Then per-ADR gates.
  D. Per-domain trios — for each approved domain ADR, spawn a child
     ``Task(complexity=COMPLEX_LARGE, parent_task_id=<parent>)``. Each
     opens its own PR.
  E. Final verification — once every child is terminal, run
     ``verify_primitives`` across the integrated whole. On
     ``gaps_found`` (bounded ≤3 rounds), spawn fix children.

The driver lives in :mod:`agent.lifecycle.scaffold.parent`. The phase
modules expose ``async def run(task)`` functions; the driver dispatches
them and handles transitions.
"""

from __future__ import annotations

from agent.lifecycle.scaffold.parent import run_scaffold_parent

__all__ = ["run_scaffold_parent"]
