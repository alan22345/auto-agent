"""PR-description ``allow-stub`` surfacing — ADR-015 §8 / Phase 9.

When a PR is opened whose diff contains lines annotated with
``# auto-agent: allow-stub``, those intentional opt-outs must be visible
to the human (or improvement-agent standin) reviewing the PR. The
surfacing happens by appending an ``## Allow-stub opt-outs in this PR``
section to the PR body before ``gh pr create`` runs.

This module is the authoritative spec for two pure helpers in
:mod:`agent.lifecycle.verify_primitives`:

  * :func:`collect_allow_stub_optouts` — given a unified diff, returns the
    list of allow-stub locations (file, line, surrounding context).
  * :func:`format_allow_stub_section` — given that list, returns the
    markdown bullets to append to a PR body. Empty list → empty string
    (PRs without allow-stub get no extra section).
"""

from __future__ import annotations

import textwrap


def test_collect_allow_stub_optouts_finds_single_location() -> None:
    from agent.lifecycle.verify_primitives import collect_allow_stub_optouts

    diff = textwrap.dedent(
        """\
        diff --git a/abc/base.py b/abc/base.py
        --- a/abc/base.py
        +++ b/abc/base.py
        @@ -1,3 +1,5 @@
         class Base:
             def method(self):
        +        # Stub method — concrete subclasses override.
        +        raise NotImplementedError  # auto-agent: allow-stub
        """
    )
    optouts = collect_allow_stub_optouts(diff)
    assert len(optouts) == 1
    assert optouts[0].file == "abc/base.py"
    # The line carrying the allow-stub annotation has a real line number.
    assert optouts[0].line >= 1
    assert "raise NotImplementedError" in optouts[0].snippet


def test_collect_allow_stub_optouts_skips_non_optout_stubs() -> None:
    """Stubs without ``# auto-agent: allow-stub`` are NOT opt-outs."""
    from agent.lifecycle.verify_primitives import collect_allow_stub_optouts

    diff = textwrap.dedent(
        """\
        diff --git a/foo.py b/foo.py
        --- a/foo.py
        +++ b/foo.py
        @@ -1,1 +1,2 @@
         x = 1
        +    raise NotImplementedError
        """
    )
    assert collect_allow_stub_optouts(diff) == []


def test_collect_allow_stub_optouts_multi_file() -> None:
    from agent.lifecycle.verify_primitives import collect_allow_stub_optouts

    diff = textwrap.dedent(
        """\
        diff --git a/a.py b/a.py
        --- a/a.py
        +++ b/a.py
        @@ -1,1 +1,2 @@
         x = 1
        +    raise NotImplementedError  # auto-agent: allow-stub
        diff --git a/b.py b/b.py
        --- a/b.py
        +++ b/b.py
        @@ -1,1 +1,2 @@
         y = 2
        +    pass  # placeholder  # auto-agent: allow-stub
        """
    )
    optouts = collect_allow_stub_optouts(diff)
    files = {o.file for o in optouts}
    assert files == {"a.py", "b.py"}
    assert len(optouts) == 2


def test_format_allow_stub_section_with_optouts() -> None:
    """Non-empty list → ``## Allow-stub opt-outs in this PR`` section."""
    from agent.lifecycle.verify_primitives import (
        AllowStubOptout,
        format_allow_stub_section,
    )

    optouts = [
        AllowStubOptout(
            file="abc/base.py",
            line=5,
            snippet="raise NotImplementedError  # auto-agent: allow-stub",
        ),
        AllowStubOptout(
            file="foo.py", line=12, snippet="pass  # placeholder  # auto-agent: allow-stub"
        ),
    ]
    section = format_allow_stub_section(optouts)
    assert section.startswith("## Allow-stub opt-outs in this PR")
    assert "abc/base.py:5" in section
    assert "foo.py:12" in section
    # Each opt-out is its own bullet.
    assert section.count("\n- ") == 2


def test_format_allow_stub_section_empty_list() -> None:
    """Empty list → empty string (no section appended)."""
    from agent.lifecycle.verify_primitives import format_allow_stub_section

    assert format_allow_stub_section([]) == ""


def test_pr_body_surfacing_appends_section(tmp_path) -> None:
    """The PR-body augmentation helper that ``_open_pr_and_advance``
    calls must append the allow-stub section when present, and leave
    the body untouched otherwise."""
    from agent.lifecycle.verify_primitives import augment_pr_body_with_optouts

    base_body = "## Auto-Agent Task #1\n\n**Task:** test\n"
    diff_clean = (
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,1 @@\n+x = 1\n"
    )
    diff_optout = textwrap.dedent(
        """\
        diff --git a/foo.py b/foo.py
        --- a/foo.py
        +++ b/foo.py
        @@ -1,1 +1,2 @@
         x = 1
        +    raise NotImplementedError  # auto-agent: allow-stub
        """
    )

    # Clean diff → body unchanged.
    assert augment_pr_body_with_optouts(base_body, diff_clean) == base_body

    # Opt-out diff → section appended.
    augmented = augment_pr_body_with_optouts(base_body, diff_optout)
    assert augmented.startswith(base_body)
    assert "## Allow-stub opt-outs in this PR" in augmented
    assert "foo.py" in augmented
