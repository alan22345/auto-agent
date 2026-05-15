"""ADR-017 — TaskStatus.ITERATING + TrioPhase.ARCHITECT_ITERATING."""

from __future__ import annotations

from shared.models import TaskStatus, TrioPhase


def test_task_status_iterating_exists():
    assert TaskStatus.ITERATING.value == "iterating"


def test_trio_phase_architect_iterating_exists():
    assert TrioPhase.ARCHITECT_ITERATING.value == "architect_iterating"
