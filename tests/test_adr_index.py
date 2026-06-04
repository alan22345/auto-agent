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
