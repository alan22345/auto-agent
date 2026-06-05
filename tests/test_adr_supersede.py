# tests/test_adr_supersede.py
from agent.context.adr_index import parse_adr, status_kind
from agent.tools.adr_supersede import retire_adr


def test_retire_adr_flips_status_and_keeps_file(tmp_path):
    p = tmp_path / "005-existing.md"
    p.write_text("# [ADR-005] Existing\n\n## Status\n\nAccepted\n\n## Context\n\nc\n")

    ok = retire_adr(str(tmp_path), 5, by_number=21)
    assert ok is True
    assert p.exists()                       # never deleted

    meta = parse_adr(str(p))
    assert status_kind(meta.status) == "superseded"
    assert meta.superseded_by == 21


def test_retire_adr_returns_false_when_missing(tmp_path):
    assert retire_adr(str(tmp_path), 99, by_number=21) is False


def test_retire_adr_returns_false_on_malformed_status(tmp_path):
    """No status value line under '## Status' → honest False, file untouched."""
    p = tmp_path / "005-malformed.md"
    original = "# [ADR-005] Malformed\n\n## Status\n"
    p.write_text(original)

    assert retire_adr(str(tmp_path), 5, by_number=21) is False
    assert p.read_text() == original  # not rewritten
