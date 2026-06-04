import textwrap
from pathlib import Path

from agent.context.adr_index import parse_adr, status_kind


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body).lstrip("\n"))
    return p


def test_parse_adr_extracts_number_title_status_summary(tmp_path):
    p = _write(tmp_path, "005-workspace-path-tool-seam.md", """
        # [ADR-005] Workspace path resolution as a single tool seam

        > **Summary:** All file tools resolve paths through one seam.

        ## Status

        Accepted

        ## Decision

        Deepen ToolContext.
    """)
    meta = parse_adr(str(p))
    assert meta.number == 5
    assert meta.title == "Workspace path resolution as a single tool seam"
    assert meta.summary == "All file tools resolve paths through one seam."
    assert status_kind(meta.status) == "accepted"
    assert meta.superseded_by is None


def test_parse_adr_reads_superseded_status(tmp_path):
    p = _write(tmp_path, "013-trio-subagents.md", """
        # [ADR-013] Trio drives its backlog via subagents

        ## Status

        Superseded by [ADR-015] — reshaped by the heavy reviewer.

        ## Context

        old.
    """)
    meta = parse_adr(str(p))
    assert status_kind(meta.status) == "superseded"
    assert meta.superseded_by == 15
    assert meta.summary is None


from agent.context.adr_index import active_adrs, build_index


def test_build_index_is_status_aware(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "000-template.md").write_text("# [ADR-NNN] Title\n")  # ignored
    _write(d, "005-seam.md",
           "# [ADR-005] Path seam\n\n> **Summary:** One resolver.\n\n## Status\n\nAccepted\n")
    _write(d, "013-old.md",
           "# [ADR-013] Old trio\n\n## Status\n\nSuperseded by [ADR-015]\n")
    _write(d, "015-flow.md",
           "# [ADR-015] Task flow\n\n> **Summary:** Three flows.\n\n## Status\n\nAccepted\n")

    active = active_adrs(str(d))
    nums = [m.number for m in active]
    assert nums == [5, 15]            # 000 skipped, 013 (superseded) omitted, sorted

    index = build_index(str(d))
    assert "ADR-005" in index and "One resolver." in index
    assert "ADR-015" in index and "Three flows." in index
    assert "ADR-013" not in index    # superseded never appears in the active index
