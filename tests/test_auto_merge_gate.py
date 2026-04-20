"""Tests for the auto-merge safety gate.

Incident context: cardamon's FreeformConfig.enabled was False, but task #50's
`freeform_mode` was True (due to a manual DB flip during a retry). When CI
passed, `on_ci_passed` checked only the task-level flag and auto-merged the
PR to prod without human review. This test pins the invariant: auto-merge
requires BOTH the task-level flag AND the repo-level freeform config
enabled — either one missing falls through to human review.
"""

from dataclasses import dataclass

import pytest

from run import _should_auto_merge


@dataclass
class StubTask:
    freeform_mode: bool


@dataclass
class StubConfig:
    enabled: bool


class TestShouldAutoMerge:
    def test_both_true_merges(self):
        """The only combination that auto-merges."""
        assert _should_auto_merge(StubTask(freeform_mode=True), StubConfig(enabled=True)) is True

    def test_task_freeform_false_does_not_merge(self):
        assert _should_auto_merge(StubTask(freeform_mode=False), StubConfig(enabled=True)) is False

    def test_repo_disabled_does_not_merge(self):
        """The incident case: task had freeform flag but repo was disabled."""
        assert _should_auto_merge(StubTask(freeform_mode=True), StubConfig(enabled=False)) is False

    def test_both_false_does_not_merge(self):
        assert _should_auto_merge(StubTask(freeform_mode=False), StubConfig(enabled=False)) is False

    def test_no_config_does_not_merge(self):
        """Repos without a FreeformConfig row at all should never auto-merge."""
        assert _should_auto_merge(StubTask(freeform_mode=True), None) is False
        assert _should_auto_merge(StubTask(freeform_mode=False), None) is False


class TestIncidentReproduction:
    """Mirrors the cardamon / task 50 state exactly."""

    def test_cardamon_task_50_would_not_have_merged(self):
        # Cardamon: freeform_config.enabled = False
        # Task 50: freeform_mode = True (manually flipped in DB)
        cardamon_config = StubConfig(enabled=False)
        task_50 = StubTask(freeform_mode=True)
        assert _should_auto_merge(task_50, cardamon_config) is False, (
            "Task 50 must NOT auto-merge — cardamon's FreeformConfig.enabled is False. "
            "This is the safety invariant that was violated."
        )
