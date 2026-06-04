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
