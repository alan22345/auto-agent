"""Tests for the deploy-failure retry gate.

When dev deployment fails we used to unconditionally re-queue coding,
which produced infinite retry loops on environmental failures (AWS
billing block, missing CLI on the runner, etc.) where the agent has no
fix available in the repo. See task #116 (2026-04-30).

Two gates now stop the loop:
  - Environmental failures are classified by `_classify_deploy_failure`
    and block on the first occurrence.
  - All other failures are capped by `_should_block_after_repeated_failures`
    after `DEPLOY_RETRY_LIMIT` total deploy-failed history entries.
"""

from dataclasses import dataclass

from run import (
    DEPLOY_RETRY_LIMIT,
    _classify_deploy_failure,
    _should_block_after_repeated_failures,
)


@dataclass
class StubHistory:
    message: str = ""


class TestClassifyDeployFailure:
    def test_empty_output_returns_none(self):
        assert _classify_deploy_failure("") is None
        assert _classify_deploy_failure(None) is None  # type: ignore[arg-type]

    def test_unknown_failure_returns_none(self):
        assert _classify_deploy_failure("ImportError: no module named foo") is None
        assert _classify_deploy_failure("pytest exit 1, 3 tests failed") is None

    def test_billing_disabled(self):
        out = "ERROR: Billing has been disabled on this account. Re-enable to continue."
        assert _classify_deploy_failure(out) is not None

    def test_payment_required(self):
        assert _classify_deploy_failure("HTTP 402 Payment Required") is not None

    def test_aws_subscription_suspended(self):
        out = "Your AWS subscription is suspended due to unpaid invoices"
        assert _classify_deploy_failure(out) is not None

    def test_insufficient_funds(self):
        assert _classify_deploy_failure("Insufficient funds in account") is not None

    def test_quota_exceeded(self):
        assert _classify_deploy_failure("ERROR: quota exceeded for resource cpus") is not None

    def test_aws_cli_missing(self):
        out = "==> Deploying...\nscripts/deploy-aws-dev.sh: line 12: aws: command not found"
        assert _classify_deploy_failure(out) is not None

    def test_docker_cli_missing(self):
        assert _classify_deploy_failure("docker: command not found") is not None

    def test_unable_to_locate_credentials(self):
        out = "Unable to locate credentials. You can configure credentials by running..."
        assert _classify_deploy_failure(out) is not None

    def test_legitimate_code_error_not_flagged(self):
        # Real code/test failures must NOT be classified as environmental
        # — those should still trigger a retry so the agent can fix them.
        assert _classify_deploy_failure("AssertionError: expected 5 got 4") is None
        assert _classify_deploy_failure("SyntaxError: invalid syntax at line 10") is None
        assert _classify_deploy_failure("npm ERR! 404 Not Found - GET https://...") is None


class TestShouldBlockAfterRepeatedFailures:
    def _hist(self, *messages: str):
        # Caller passes most-recent-first, matching the production query.
        return [StubHistory(message=m) for m in messages]

    def test_no_history_does_not_block(self):
        assert _should_block_after_repeated_failures([]) is False

    def test_first_failure_does_not_block(self):
        # No prior deploy-failed entries — let the retry happen.
        h = self._hist("Plan approved by user", "PR created: ...")
        assert _should_block_after_repeated_failures(h) is False

    def test_second_failure_does_not_block(self):
        # One prior failure — give the agent one more chance.
        h = self._hist("Deploy failed — fetched logs", "PR created: ...")
        assert _should_block_after_repeated_failures(h) is False

    def test_third_failure_blocks(self):
        # Two prior failures already — this would be the third. Block.
        h = self._hist(
            "Deploy failed — fetched logs",
            "CI passed, awaiting human review",
            "Deploy failed — fetched logs",
            "PR created: ...",
        )
        assert _should_block_after_repeated_failures(h) is True

    def test_limit_constant_is_sane(self):
        # A regression guard — if someone bumps this very high, the gate
        # stops being effective.
        assert 1 <= DEPLOY_RETRY_LIMIT <= 5
