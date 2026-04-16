"""Tests for agent/tools/grep_tool.py — context lines and multiline support."""

import os

import pytest

from agent.tools.base import ToolContext
from agent.tools.grep_tool import GrepTool


@pytest.fixture
def tool():
    return GrepTool()


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "app.py").write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "def main():\n"
        "    print('hello')\n"
        "    return 0\n"
        "\n"
        "def helper():\n"
        "    pass\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    (tmp_path / "models.py").write_text(
        "class User:\n"
        "    name: str\n"
        "    email: str\n"
        "\n"
        "class Admin(User):\n"
        "    role: str\n"
    )
    return str(tmp_path)


@pytest.fixture
def ctx(workspace):
    return ToolContext(workspace=workspace)


class TestBasicGrep:
    @pytest.mark.asyncio
    async def test_finds_pattern(self, tool, ctx):
        result = await tool.execute({"pattern": "def main"}, ctx)
        assert not result.is_error
        assert "app.py" in result.output
        assert "def main" in result.output

    @pytest.mark.asyncio
    async def test_no_matches(self, tool, ctx):
        result = await tool.execute({"pattern": "nonexistent_xyz"}, ctx)
        assert "No matches" in result.output

    @pytest.mark.asyncio
    async def test_case_insensitive(self, tool, ctx):
        result = await tool.execute(
            {"pattern": "CLASS user", "case_insensitive": True}, ctx
        )
        assert "User" in result.output

    @pytest.mark.asyncio
    async def test_glob_filter(self, tool, ctx):
        result = await tool.execute(
            {"pattern": "class", "glob": "*.py"}, ctx
        )
        assert "models.py" in result.output


class TestContextLines:
    @pytest.mark.asyncio
    async def test_context_lines_shows_surrounding(self, tool, ctx):
        result = await tool.execute(
            {"pattern": "def main", "context_lines": 2}, ctx
        )
        assert not result.is_error
        # Should show 2 lines before and after "def main"
        assert "import sys" in result.output or "import os" in result.output
        assert "print" in result.output

    @pytest.mark.asyncio
    async def test_context_lines_marks_match(self, tool, ctx):
        result = await tool.execute(
            {"pattern": "def main", "context_lines": 1}, ctx
        )
        # Match lines should be prefixed with >
        lines = result.output.splitlines()
        match_lines = [l for l in lines if ">" in l and "def main" in l]
        assert len(match_lines) >= 1

    @pytest.mark.asyncio
    async def test_context_lines_zero_is_normal(self, tool, ctx):
        result = await tool.execute(
            {"pattern": "def main", "context_lines": 0}, ctx
        )
        # Should just show the matching line, no context
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert len(lines) == 1
        assert "def main" in lines[0]

    @pytest.mark.asyncio
    async def test_context_lines_capped_at_10(self, tool, ctx):
        # Requesting 100 should be capped to 10
        result = await tool.execute(
            {"pattern": "def main", "context_lines": 100}, ctx
        )
        assert not result.is_error


class TestMultiline:
    @pytest.mark.asyncio
    async def test_multiline_matches_across_lines(self, tool, ctx):
        result = await tool.execute(
            {"pattern": "class User.*?email", "multiline": True}, ctx
        )
        assert not result.is_error
        assert "models.py" in result.output

    @pytest.mark.asyncio
    async def test_multiline_shows_line_range(self, tool, ctx):
        result = await tool.execute(
            {"pattern": "def main.*?return 0", "multiline": True}, ctx
        )
        assert not result.is_error
        # Should show a line range like "4-6"
        assert "-" in result.output  # Line range indicator

    @pytest.mark.asyncio
    async def test_multiline_with_case_insensitive(self, tool, ctx):
        result = await tool.execute(
            {"pattern": "CLASS USER.*?email", "multiline": True, "case_insensitive": True}, ctx
        )
        assert not result.is_error
        assert "models.py" in result.output


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_invalid_regex(self, tool, ctx):
        result = await tool.execute({"pattern": "["}, ctx)
        assert result.is_error
        assert "invalid regex" in result.output

    @pytest.mark.asyncio
    async def test_path_escape_blocked(self, tool, ctx):
        result = await tool.execute({"pattern": "x", "path": "../../etc"}, ctx)
        assert result.is_error
        assert "escapes" in result.output

    @pytest.mark.asyncio
    async def test_search_specific_file(self, tool, ctx, workspace):
        result = await tool.execute(
            {"pattern": "class", "path": "models.py"}, ctx
        )
        assert not result.is_error
        assert "User" in result.output
        # Should NOT include results from app.py
        assert "app.py" not in result.output
