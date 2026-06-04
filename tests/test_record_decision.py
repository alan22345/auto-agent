# tests/test_record_decision.py
import pytest

from agent.tools.base import ToolContext
from agent.tools.record_decision import RecordDecisionTool


@pytest.mark.asyncio
async def test_record_decision_writes_prefixed_title_summary_and_index(tmp_path):
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    (d / "000-template.md").write_text("# [ADR-NNN] Title\n\n## Status\n\nProposed\n")
    (d / "005-existing.md").write_text(
        "# [ADR-005] Existing\n\n> **Summary:** Keeps things tidy.\n\n## Status\n\nAccepted\n")

    tool = RecordDecisionTool()
    ctx = ToolContext(workspace=str(tmp_path))
    res = await tool.execute(
        {"title": "New caching layer", "context": "c", "decision": "d",
         "consequences": "x", "summary": "Cache responses at the edge."},
        ctx,
    )
    assert not res.is_error
    new = (d / "006-new-caching-layer.md").read_text()
    assert new.startswith("# [ADR-006] New caching layer")
    assert "> **Summary:** Cache responses at the edge." in new

    index = (d / "INDEX.md").read_text()
    assert "ADR-006" in index and "Cache responses at the edge." in index
    assert "ADR-005" in index


@pytest.mark.asyncio
async def test_record_decision_supersedes_retires_old_and_drops_it_from_index(tmp_path):
    """supersedes=N retires ADR-N in the same write and removes it from INDEX."""
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    (d / "000-template.md").write_text("# [ADR-NNN] Title\n\n## Status\n\nProposed\n")
    (d / "005-existing.md").write_text(
        "# [ADR-005] Old approach\n\n> **Summary:** The old way.\n\n## Status\n\nAccepted\n")

    tool = RecordDecisionTool()
    ctx = ToolContext(workspace=str(tmp_path))
    res = await tool.execute(
        {"title": "Replacement approach", "context": "c", "decision": "d",
         "consequences": "x", "summary": "The new way.", "supersedes": 5},
        ctx,
    )
    assert not res.is_error
    assert "superseded ADR-005" in res.output

    # ADR-005 is soft-retired in place (file kept, status flipped to the new ADR).
    old = (d / "005-existing.md").read_text()
    assert "Superseded by [ADR-006]" in old

    # INDEX.md now lists the new ADR and omits the retired one.
    index = (d / "INDEX.md").read_text()
    assert "ADR-006" in index
    assert "ADR-005" not in index
