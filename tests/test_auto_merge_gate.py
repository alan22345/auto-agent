"""Tests for the auto-merge gate.

All tasks auto-merge to dev after CI passes, as long as the repo has a
dev branch configured (via FreeformConfig). No freeform_mode flag needed.
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
    def test_config_with_dev_branch_merges(self):
        """Any task auto-merges if repo has a dev branch configured."""
        assert _should_auto_merge(StubTask(), StubConfig(dev_branch="dev")) is True

    def test_freeform_task_also_merges(self):
        assert _should_auto_merge(StubTask(freeform_mode=True), StubConfig(dev_branch="dev")) is True

    def test_no_config_does_not_merge(self):
        """Repos without a FreeformConfig row should not auto-merge."""
        assert _should_auto_merge(StubTask(), None) is False
        assert _should_auto_merge(StubTask(freeform_mode=True), None) is False

    def test_empty_dev_branch_does_not_merge(self):
        """If dev_branch is empty string, don't auto-merge."""
        assert _should_auto_merge(StubTask(), StubConfig(dev_branch="")) is False

    def test_config_with_dev_branch_regardless_of_enabled(self):
        """enabled flag doesn't matter — only dev_branch presence matters."""
        assert _should_auto_merge(StubTask(), StubConfig(enabled=False, dev_branch="dev")) is True
        assert _should_auto_merge(StubTask(), StubConfig(enabled=True, dev_branch="dev")) is True
