"""Tests for _pr_base_branch_for_task — verifies that trio children target
the parent's integration branch, freeform tasks target their dev branch,
and regular tasks target main.
"""

import pytest


@pytest.mark.asyncio
async def test_trio_child_pr_targets_parent_integration_branch():
    """A trio child's PR base branch is trio/<parent_id>."""
    from agent.lifecycle.coding import _pr_base_branch_for_task

    # Simple synthetic task object — no DB required for this helper.
    class T:
        parent_task_id = 42
        freeform_mode = True
        repo = None

    base = await _pr_base_branch_for_task(T())
    assert base == "trio/42"


@pytest.mark.asyncio
async def test_freeform_non_trio_pr_targets_dev_branch():
    """A freeform task with no parent and a freeform_config targets the config's dev_branch."""
    from agent.lifecycle.coding import _pr_base_branch_for_task

    class FreeformConfig:
        dev_branch = "develop"

    class Repo:
        freeform_config = FreeformConfig()

    class T:
        parent_task_id = None
        freeform_mode = True
        repo = Repo()

    base = await _pr_base_branch_for_task(T())
    assert base == "develop"


@pytest.mark.asyncio
async def test_non_trio_non_freeform_pr_targets_main():
    """A regular non-trio non-freeform task targets main."""
    from agent.lifecycle.coding import _pr_base_branch_for_task

    class T:
        parent_task_id = None
        freeform_mode = False
        repo = None

    base = await _pr_base_branch_for_task(T())
    assert base == "main"


@pytest.mark.asyncio
async def test_freeform_no_freeform_config_falls_back_to_dev():
    """Freeform without a freeform_config still defaults to 'dev' (existing legacy behaviour)."""
    from agent.lifecycle.coding import _pr_base_branch_for_task

    class Repo:
        freeform_config = None

    class T:
        parent_task_id = None
        freeform_mode = True
        repo = Repo()

    base = await _pr_base_branch_for_task(T())
    assert base in ("dev", "develop", "main")
    # The implementation returns "dev" for freeform tasks with no config.
    assert base == "dev"
