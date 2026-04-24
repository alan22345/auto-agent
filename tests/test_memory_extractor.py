import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.llm.types import LLMResponse, Message
from agent.memory_extractor import extract


def _mock_provider(text_response: str):
    provider = MagicMock()
    response = LLMResponse(
        message=Message(role="assistant", content=text_response),
        stop_reason="end_turn",
    )
    provider.complete = AsyncMock(return_value=response)
    return provider


async def test_extract_parses_valid_json():
    payload = json.dumps({"facts": [
        {"entity": "auto-agent", "entity_type": "project",
         "kind": "decision", "content": "PO runs nightly"},
        {"entity": "pg-migrations", "entity_type": "concept",
         "kind": "gotcha", "content": "run 018 before 019"},
    ]})
    rows = await extract(
        text="some source text",
        hint=None,
        existing_facts_by_entity={},
        provider=_mock_provider(payload),
    )
    assert len(rows) == 2
    assert rows[0].entity == "auto-agent"
    assert rows[0].kind == "decision"
    assert rows[0].conflicts == []
    assert len({r.row_id for r in rows}) == 2


async def test_extract_fallback_kind_when_missing():
    payload = json.dumps({"facts": [
        {"entity": "e", "entity_type": "concept", "content": "no kind here"}
    ]})
    rows = await extract("t", None, {}, _mock_provider(payload))
    assert rows[0].kind == "fact"


async def test_extract_retries_on_bad_json():
    provider = MagicMock()
    bad = LLMResponse(message=Message(role="assistant", content="not json at all"), stop_reason="end_turn")
    good = LLMResponse(message=Message(role="assistant", content=json.dumps({"facts": [
        {"entity": "e", "entity_type": "concept", "kind": "fact", "content": "c"}
    ]})), stop_reason="end_turn")
    provider.complete = AsyncMock(side_effect=[bad, good])
    rows = await extract("t", None, {}, provider)
    assert len(rows) == 1
    assert provider.complete.await_count == 2


async def test_extract_raises_after_two_bad_attempts():
    provider = MagicMock()
    bad = LLMResponse(message=Message(role="assistant", content="still not json"), stop_reason="end_turn")
    provider.complete = AsyncMock(return_value=bad)
    with pytest.raises(ValueError, match="could not parse"):
        await extract("t", None, {}, provider)


async def test_extract_marks_conflicts():
    payload = json.dumps({"facts": [
        {"entity": "auto-agent", "entity_type": "project",
         "kind": "status", "content": "PO runs hourly",
         "conflicts_with": ["f-existing-1"]}
    ]})
    rows = await extract(
        text="...",
        hint=None,
        existing_facts_by_entity={
            "auto-agent": [{"id": "f-existing-1", "content": "PO runs nightly", "kind": "status"}]
        },
        provider=_mock_provider(payload),
    )
    assert len(rows[0].conflicts) == 1
    assert rows[0].conflicts[0].fact_id == "f-existing-1"
    assert rows[0].conflicts[0].existing_content == "PO runs nightly"
