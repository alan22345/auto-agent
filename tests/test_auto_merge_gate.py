"""Tests for the auto-merge gate.

Only freeform tasks auto-merge to dev after CI passes. Non-freeform tasks
always go through human review, even if the repo has a dev branch configured.
"""

from dataclasses import dataclass

from run import _should_auto_merge


@dataclass
class StubTask:
    freeform_mode: bool = False


@dataclass
class StubConfig:
    enabled: bool = False
    dev_branch: str = "dev"


class TestShouldAutoMerge:
    def test_freeform_task_with_dev_branch_merges(self):
        """Freeform tasks auto-merge if repo has a dev branch configured."""
        assert _should_auto_merge(StubTask(freeform_mode=True), StubConfig(dev_branch="dev")) is True

    def test_non_freeform_task_does_not_merge(self):
        """Non-freeform tasks should not auto-merge, even with dev branch."""
        assert _should_auto_merge(StubTask(freeform_mode=False), StubConfig(dev_branch="dev")) is False

    def test_no_config_does_not_merge(self):
        """Repos without a FreeformConfig row should not auto-merge."""
        assert _should_auto_merge(StubTask(), None) is False
        assert _should_auto_merge(StubTask(freeform_mode=True), None) is False

    def test_empty_dev_branch_does_not_merge(self):
        """If dev_branch is empty string, don't auto-merge even for freeform."""
        assert _should_auto_merge(StubTask(freeform_mode=True), StubConfig(dev_branch="")) is False

    def test_freeform_merges_regardless_of_enabled_flag(self):
        """enabled flag doesn't matter — only freeform_mode + dev_branch presence matters."""
        assert _should_auto_merge(StubTask(freeform_mode=True), StubConfig(enabled=False, dev_branch="dev")) is True
        assert _should_auto_merge(StubTask(freeform_mode=True), StubConfig(enabled=True, dev_branch="dev")) is True
