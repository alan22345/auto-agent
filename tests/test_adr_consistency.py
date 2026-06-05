from agent.lifecycle.verify_primitives import check_adr_consistency


def _seed(tmp_path):
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    return d


def test_new_adr_supersedes_but_old_left_accepted_is_flagged(tmp_path):
    d = _seed(tmp_path)
    (d / "005-old.md").write_text("# [ADR-005] Old\n\n## Status\n\nAccepted\n")
    (d / "023-new.md").write_text(
        "# [ADR-023] New\n\n> **Summary:** Replaces old.\n\n## Status\n\nAccepted\n\n"
        "## Decision\n\nSupersedes [ADR-005].\n"
    )
    diff = "diff --git a/docs/decisions/023-new.md b/docs/decisions/023-new.md\n+Supersedes [ADR-005].\n"

    result = check_adr_consistency(diff, str(d))
    assert not result.ok
    assert any("005" in v for v in result.violations)


def test_new_adr_supersedes_and_old_retired_is_ok(tmp_path):
    d = _seed(tmp_path)
    (d / "005-old.md").write_text("# [ADR-005] Old\n\n## Status\n\nSuperseded by [ADR-023]\n")
    (d / "023-new.md").write_text(
        "# [ADR-023] New\n\n## Status\n\nAccepted\n\n## Decision\n\nSupersedes [ADR-005].\n"
    )
    diff = "diff --git a/docs/decisions/023-new.md b/docs/decisions/023-new.md\n+Supersedes [ADR-005].\n"

    result = check_adr_consistency(diff, str(d))
    assert result.ok
    assert result.violations == []


def test_supersedes_mention_outside_adr_file_is_not_flagged(tmp_path):
    """A 'supersedes [ADR-X]' mention in a non-ADR file (comment, PR echo) must
    NOT trip the gate — only added lines inside docs/decisions/*.md count."""
    d = _seed(tmp_path)
    (d / "005-old.md").write_text("# [ADR-005] Old\n\n## Status\n\nAccepted\n")
    diff = (
        "diff --git a/agent/foo.py b/agent/foo.py\n"
        "--- a/agent/foo.py\n"
        "+++ b/agent/foo.py\n"
        "@@ -1 +1,2 @@\n"
        "+# this supersedes [ADR-005] eventually\n"
    )

    result = check_adr_consistency(diff, str(d))
    assert result.ok
    assert result.violations == []


def test_supersedes_in_diff_git_header_filename_is_not_flagged(tmp_path):
    """A '+++'/'diff --git' header that merely contains the word must not match."""
    d = _seed(tmp_path)
    (d / "005-old.md").write_text("# [ADR-005] Old\n\n## Status\n\nAccepted\n")
    # Non-ADR file whose path coincidentally contains the phrase.
    diff = (
        "diff --git a/notes/supersedes-adr-005.md b/notes/supersedes-adr-005.md\n"
        "+++ b/notes/supersedes-adr-005.md\n"
        "+some unrelated content\n"
    )

    result = check_adr_consistency(diff, str(d))
    assert result.ok
