"""Tests for the deploy-failure diagnostic fallback.

When the deploy script fails with short output AND the PR has no failed
GitHub Actions check runs (typical for repos that deploy outside CI), the
old code surfaced "No failed check runs found" to the retry loop and
discarded the actual deploy-script output. The retry loop then had nothing
actionable to fix.

The fix:
  - _fetch_failed_ci_logs returns one of a known set of sentinel strings
    when it couldn't surface a real diagnostic.
  - on_dev_deploy_failed checks _ci_logs_are_empty and falls back to the
    deploy-script output when the sentinel is hit.
"""

from __future__ import annotations

from run import _ci_logs_are_empty, _EMPTY_CI_LOG_SENTINELS


def test_known_sentinels_are_recognised_as_empty():
    """Every advertised sentinel must be detected by _ci_logs_are_empty."""
    for sentinel in _EMPTY_CI_LOG_SENTINELS:
        assert _ci_logs_are_empty(sentinel), f"{sentinel!r} not detected"
        # Whitespace tolerance — fetcher may add a trailing newline.
        assert _ci_logs_are_empty(f"  {sentinel}  \n")


def test_real_log_content_not_treated_as_empty():
    """A real failed-check log must NOT be treated as empty."""
    real_log = "## Failed: tests\n  src/foo.py:42: AssertionError"
    assert not _ci_logs_are_empty(real_log)


def test_unknown_short_string_not_treated_as_empty():
    """Defensive — only the exact known sentinels are 'empty'."""
    assert not _ci_logs_are_empty("Something else broke")
    assert not _ci_logs_are_empty("")  # empty string isn't a sentinel
