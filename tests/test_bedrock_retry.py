"""Tests for the retry logic in BedrockProvider."""

from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from agent.llm.bedrock import BedrockProvider, _MAX_RETRIES
from agent.llm.types import Message


def _api_status_error(status_code: int) -> anthropic.APIStatusError:
    """Helper to construct an APIStatusError with the given status code."""
    request = httpx.Request("POST", "https://bedrock.example/v1/messages")
    response = httpx.Response(status_code, request=request)
    return anthropic.APIStatusError(
        message=f"Error {status_code}",
        response=response,
        body={"message": f"Error {status_code}"},
    )


@pytest.fixture
def provider():
    p = BedrockProvider(region="us-east-1", model="claude-sonnet-4-6")
    return p


class TestRetryLogic:
    @pytest.mark.asyncio
    async def test_retries_on_503(self, provider):
        """A 503 error should trigger retries with backoff."""
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _api_status_error(503)
            # Success on third attempt
            mock_resp = MagicMock()
            mock_resp.content = [MagicMock(type="text", text="success")]
            mock_resp.stop_reason = "end_turn"
            mock_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
            return mock_resp

        provider._client.messages = MagicMock()
        provider._client.messages.create = mock_create

        # Patch asyncio.sleep to skip actual backoff waits
        with patch("agent.llm.bedrock.asyncio.sleep", new=AsyncMock()):
            response = await provider.complete(
                messages=[Message(role="user", content="hi")],
            )

        assert response.message.content == "success"
        assert call_count == 3, "Should have retried twice before succeeding"

    @pytest.mark.asyncio
    async def test_retries_on_429(self, provider):
        """429 (rate limit) should also be retried."""
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise _api_status_error(429)
            mock_resp = MagicMock()
            mock_resp.content = [MagicMock(type="text", text="ok")]
            mock_resp.stop_reason = "end_turn"
            mock_resp.usage = MagicMock(input_tokens=5, output_tokens=2)
            return mock_resp

        provider._client.messages = MagicMock()
        provider._client.messages.create = mock_create

        with patch("agent.llm.bedrock.asyncio.sleep", new=AsyncMock()):
            response = await provider.complete(
                messages=[Message(role="user", content="test")],
            )

        assert response.message.content == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_on_400(self, provider):
        """400 (client error) should NOT be retried — raise immediately."""
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            raise _api_status_error(400)

        provider._client.messages = MagicMock()
        provider._client.messages.create = mock_create

        with patch("agent.llm.bedrock.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(anthropic.APIStatusError):
                await provider.complete(
                    messages=[Message(role="user", content="test")],
                )

        assert call_count == 1, "Should not retry on 400"

    @pytest.mark.asyncio
    async def test_does_not_retry_on_401(self, provider):
        """401 (auth) should NOT be retried."""
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            raise _api_status_error(401)

        provider._client.messages = MagicMock()
        provider._client.messages.create = mock_create

        with patch("agent.llm.bedrock.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(anthropic.APIStatusError):
                await provider.complete(
                    messages=[Message(role="user", content="test")],
                )

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self, provider):
        """If 503 persists, give up after _MAX_RETRIES and re-raise."""
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            raise _api_status_error(503)

        provider._client.messages = MagicMock()
        provider._client.messages.create = mock_create

        with patch("agent.llm.bedrock.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(anthropic.APIStatusError):
                await provider.complete(
                    messages=[Message(role="user", content="test")],
                )

        # Initial attempt + _MAX_RETRIES retries
        assert call_count == _MAX_RETRIES + 1

    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt_without_retry(self, provider):
        """Success on first call should not sleep or retry."""
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.content = [MagicMock(type="text", text="first-try")]
            mock_resp.stop_reason = "end_turn"
            mock_resp.usage = MagicMock(input_tokens=3, output_tokens=1)
            return mock_resp

        provider._client.messages = MagicMock()
        provider._client.messages.create = mock_create

        sleep_mock = AsyncMock()
        with patch("agent.llm.bedrock.asyncio.sleep", new=sleep_mock):
            response = await provider.complete(
                messages=[Message(role="user", content="test")],
            )

        assert response.message.content == "first-try"
        assert call_count == 1
        sleep_mock.assert_not_called()
