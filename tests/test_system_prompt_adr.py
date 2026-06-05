# tests/test_system_prompt_adr.py
import pytest

from agent.context.system import SystemPromptBuilder


@pytest.mark.asyncio
async def test_build_injects_active_adr_index(tmp_path):
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    (d / "005-seam.md").write_text(
        "# [ADR-005] Path seam\n\n> **Summary:** One resolver.\n\n## Status\n\nAccepted\n")
    (d / "013-old.md").write_text(
        "# [ADR-013] Old\n\n## Status\n\nSuperseded by [ADR-015]\n")

    builder = SystemPromptBuilder()
    prompt = await builder.build(workspace=str(tmp_path))

    assert "Architecture Decisions" in prompt
    assert "ADR-005" in prompt and "One resolver." in prompt
    assert "ADR-013" not in prompt
