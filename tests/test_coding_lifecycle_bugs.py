"""Regression tests for three coding-lifecycle bugs that combined to
silently fail task #156 ("collapse pre-flight compaction loop") on the
production VM.

Bug 0 — non-UUID subtask session IDs: `_handle_coding_with_subtasks`
        built `f"{session_id}-phase-{i+1}"`, which Claude Code CLI 2.1.x
        rejects with "Invalid session ID. Must be a valid UUID."

Bug 1 — CLI errors swallowed as success: when the CLI exits non-zero
        the provider returns "[ERROR] CLI exited N: …" as `result.output`.
        The lifecycle treated it as a normal subtask completion and
        marched through all phases.

Bug 2 — `REVIEW_PASSED` substring match: `if "REVIEW_PASSED" in output`
        matched the agent's own message *"Outputting `REVIEW_PASSED`
        would be misleading since there's no diff…"*. The convention
        (see prompt) is REVIEW_PASSED on a line by itself.
"""

from __future__ import annotations

import inspect
import uuid

from agent.lifecycle import coding


def test_subtask_session_ids_are_valid_uuids():
    """Bug 0: subtask session ids must parse as UUIDs.

    Reading the source guards against the regression even when the
    function is async + httpx-heavy (no easy unit-level invocation).
    """
    src = inspect.getsource(coding._handle_coding_with_subtasks)
    assert 'f"{session_id}-phase-' not in src, (
        "subtask_session must be a real UUID, not a suffixed string — "
        "Claude CLI 2.1.x rejects non-UUID session ids"
    )
    # And the replacement we expect:
    assert "_fresh_session_id(" in src


def test_is_cli_error_detects_provider_error_format():
    """Bug 1: the helper recognises every error format the CLI provider emits."""
    assert coding._is_cli_error("[ERROR] CLI exited 1: Error: Invalid session ID")
    assert coding._is_cli_error("[ERROR] Claude Code CLI timed out")
    assert not coding._is_cli_error("Did some work and committed.\nREVIEW_PASSED")
    assert not coding._is_cli_error("")


def test_review_passed_match_is_line_anchored():
    """Bug 2: REVIEW_PASSED only counts when on its own line."""
    # Positive cases — actual marker
    assert coding._review_passed("ran tests, all good\nREVIEW_PASSED\n")
    assert coding._review_passed("REVIEW_PASSED")
    assert coding._review_passed("REVIEW_PASSED  ")
    # Negative cases — incidental mention (the actual #156 failure)
    assert not coding._review_passed(
        "Outputting `REVIEW_PASSED` would be misleading since there's no diff."
    )
    assert not coding._review_passed("I will not output REVIEW_PASSED here.")
    assert not coding._review_passed("")


def test_session_id_is_a_valid_uuid_helper_exists():
    """Sanity that _fresh_session_id stays the right tool for the job."""
    from agent.lifecycle._naming import _fresh_session_id

    sid = _fresh_session_id(156, "phase-1")
    uuid.UUID(sid)  # raises if not a UUID
