from unittest.mock import AsyncMock, patch

import pytest

from shared.memory_io import correct_fact, recall_entity, remember_row
from shared.types import ProposedFact


class FakeEngine:
    def __init__(self, recall_result=None, remember_result=None, correct_result=None):
        self.recall = AsyncMock(return_value=recall_result or {"matches": [], "ambiguous": False})
        self.remember = AsyncMock(return_value=remember_result or {"fact_id": "f-new"})
        self.correct = AsyncMock(return_value=correct_result or {"fact_id": "f-upd"})


class _Ctx:
    async def __aenter__(self): return "session"
    async def __aexit__(self, *a): return False


@pytest.fixture
def patched_session():
    fake = FakeEngine(recall_result={
        "matches": [{
            "entity": {"name": "auto-agent", "type": "project"},
            "facts": [{"id": "f1", "content": "existing", "kind": "fact"}],
            "score": 0.9,
        }],
        "ambiguous": False,
    })
    with patch("shared.memory_io.team_memory_session", return_value=_Ctx()), \
         patch("shared.memory_io.GraphEngine", return_value=fake):
        yield fake


async def test_recall_entity_returns_match(patched_session):
    result = await recall_entity("auto-agent")
    assert result["entity"]["name"] == "auto-agent"
    assert result["facts"][0]["id"] == "f1"
    assert result["score"] == 0.9


async def test_recall_entity_no_match():
    fake = FakeEngine()
    with patch("shared.memory_io.team_memory_session", return_value=_Ctx()), \
         patch("shared.memory_io.GraphEngine", return_value=fake):
        result = await recall_entity("missing-thing")
    assert result is None


async def test_remember_row_calls_engine(patched_session):
    row = ProposedFact(row_id="r1", entity="e", entity_type="project", kind="fact", content="c")
    await remember_row(row, author="alan")
    patched_session.remember.assert_awaited_once()
    kwargs = patched_session.remember.await_args.kwargs
    assert kwargs["content"] == "c"
    assert kwargs["entity"] == "e"
    assert kwargs["entity_type"] == "project"
    assert kwargs["kind"] == "fact"
    assert kwargs["author"] == "alan"


async def test_correct_fact_calls_engine(patched_session):
    await correct_fact("f1", "new content", author="alan")
    patched_session.correct.assert_awaited_once()
    kwargs = patched_session.correct.await_args.kwargs
    assert kwargs["fact_id"] == "f1"
    assert kwargs["new_content"] == "new content"
    assert "reason" in kwargs
