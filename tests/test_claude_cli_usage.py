"""Verify ClaudeCLIProvider parses --output-format=json envelope and reports usage."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.llm.claude_cli import ClaudeCLIProvider
from agent.llm.types import Message


@pytest.mark.asyncio
async def test_complete_parses_usage_from_json_envelope():
    """The provider must extract input/output token counts from the result envelope."""

    envelope = {
        "type": "result",
        "subtype": "success",
        "result": "Hello from Claude.",
        "session_id": "sid-1",
        "total_cost_usd": 0.0123,
        "duration_ms": 4567,
        "usage": {
            "input_tokens": 12345,
            "output_tokens": 678,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    fake_stdout = (json.dumps(envelope) + "\n").encode()

    provider = ClaudeCLIProvider()

    async def fake_invoke(prompt: str):
        return (fake_stdout.decode(), "", 0)

    with patch.object(provider, "_invoke_cli_once", new=AsyncMock(side_effect=fake_invoke)):
        response = await provider.complete(
            messages=[Message(role="user", content="hi")],
        )

    assert response.message.content == "Hello from Claude."
    assert response.usage.input_tokens == 12345
    assert response.usage.output_tokens == 678


@pytest.mark.asyncio
async def test_complete_tolerates_non_json_stdout():
    """If parsing fails (older Claude Code), fall back to plain text + zero usage."""

    provider = ClaudeCLIProvider()

    async def fake_invoke(prompt: str):
        return ("plain text not JSON", "", 0)

    with patch.object(provider, "_invoke_cli_once", new=AsyncMock(side_effect=fake_invoke)):
        response = await provider.complete(
            messages=[Message(role="user", content="hi")],
        )

    # Plain text is returned verbatim; usage stays zero (best-effort).
    assert response.message.content == "plain text not JSON"
    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 0


@pytest.mark.asyncio
async def test_invoke_cli_passes_output_format_json_flag():
    """The CLI invocation must include --output-format=json so usage is reported."""

    captured_cmd: list[str] = []

    async def fake_create_subprocess(*cmd, **kwargs):
        captured_cmd.extend(cmd)

        class _P:
            returncode = 0

            async def communicate(self, input=None):
                return (
                    b'{"type":"result","result":"ok","usage":{"input_tokens":1,"output_tokens":1}}',
                    b"",
                )

        return _P()

    provider = ClaudeCLIProvider()
    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess):
        await provider._invoke_cli_once("hello")

    assert "--output-format" in captured_cmd
    idx = captured_cmd.index("--output-format")
    assert captured_cmd[idx + 1] == "json"
