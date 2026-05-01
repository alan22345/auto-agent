from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.memory_io import (
    correct_fact,
    delete_fact,
    get_entity_with_facts,
    list_recent_entities,
    recall_entity,
    remember_row,
    search_entities,
)
from shared.types import ProposedFact


class FakeEngine:
    def __init__(self, recall_result=None, remember_result=None, correct_result=None):
        self.recall = AsyncMock(return_value=recall_result or {"matches": [], "ambiguous": False})
        self.remember = AsyncMock(return_value=remember_result or {"fact_id": "f-new"})
        self.correct = AsyncMock(return_value=correct_result or {"fact_id": "f-upd"})


class _Ctx:
    async def __aenter__(self):
        return "session"

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def patched_session():
    fake = FakeEngine(
        recall_result={
            "matches": [
                {
                    "entity": {"name": "auto-agent", "type": "project"},
                    "facts": [{"id": "f1", "content": "existing", "kind": "fact"}],
                    "score": 0.9,
                }
            ],
            "ambiguous": False,
        }
    )
    with (
        patch("shared.memory_io.team_memory_session", return_value=_Ctx()),
        patch("shared.memory_io.GraphEngine", return_value=fake),
    ):
        yield fake


async def test_recall_entity_returns_match(patched_session):
    result = await recall_entity("auto-agent")
    assert result["entity"]["name"] == "auto-agent"
    assert result["facts"][0]["id"] == "f1"
    assert result["score"] == 0.9


async def test_recall_entity_no_match():
    fake = FakeEngine()
    with (
        patch("shared.memory_io.team_memory_session", return_value=_Ctx()),
        patch("shared.memory_io.GraphEngine", return_value=fake),
    ):
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
    assert kwargs["reason"] == "updated via memory tab"


async def test_correct_fact_passes_user_reason(patched_session):
    await correct_fact("f1", "new content", reason="ratified after review", author="alan")
    kwargs = patched_session.correct.await_args.kwargs
    assert kwargs["reason"] == "ratified after review"


# ---- search_entities ----


async def test_search_entities_empty_query_returns_empty():
    assert await search_entities("") == []
    assert await search_entities("   ") == []


async def test_search_entities_reshapes_matches():
    fake = FakeEngine(
        recall_result={
            "matches": [
                {
                    "entity": {
                        "id": "e-1",
                        "name": "auto-agent",
                        "type": "project",
                        "tags": ["agent", "ws"],
                    },
                    "facts": [
                        {
                            "id": "f1",
                            "content": "x",
                            "kind": "fact",
                            "valid_from": "2026-04-01T00:00:00+00:00",
                            "valid_until": None,
                        },
                        {
                            "id": "f2",
                            "content": "y",
                            "kind": "fact",
                            "valid_from": "2026-04-02T00:00:00+00:00",
                            "valid_until": None,
                        },
                    ],
                },
                {
                    "entity": {"id": "e-2", "name": "atlas", "type": "system", "tags": []},
                    "facts": [],
                },
            ],
            "ambiguous": False,
        }
    )
    with (
        patch("shared.memory_io.team_memory_session", return_value=_Ctx()),
        patch("shared.memory_io.GraphEngine", return_value=fake),
    ):
        out = await search_entities("auto", limit=5)
    assert len(out) == 2
    assert out[0].name == "auto-agent"
    assert out[0].fact_count == 2
    assert out[0].latest_fact_at == "2026-04-02T00:00:00+00:00"
    assert out[0].tags == ["agent", "ws"]
    assert out[1].fact_count == 0
    fake.recall.assert_awaited_once()
    assert fake.recall.await_args.kwargs["max_results"] == 5


async def test_search_entities_no_matches():
    fake = FakeEngine()  # default empty matches
    with (
        patch("shared.memory_io.team_memory_session", return_value=_Ctx()),
        patch("shared.memory_io.GraphEngine", return_value=fake),
    ):
        out = await search_entities("nope")
    assert out == []


# ---- list_recent_entities ----


class _SessionCtx:
    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *a):
        return False


async def test_list_recent_entities_returns_summaries():
    import datetime as dt

    rows = [
        SimpleNamespace(
            id="00000000-0000-0000-0000-000000000001",
            name="auto-agent",
            entity_type="project",
            tags=["agent"],
            fact_count=3,
            latest_at=dt.datetime(2026, 4, 30, 12, 0, tzinfo=dt.UTC),
        ),
        SimpleNamespace(
            id="00000000-0000-0000-0000-000000000002",
            name="atlas",
            entity_type="system",
            tags=[],
            fact_count=1,
            latest_at=None,
        ),
    ]
    session = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    with patch("shared.memory_io.team_memory_session", return_value=_SessionCtx(session)):
        out = await list_recent_entities(limit=10)
    assert [e.name for e in out] == ["auto-agent", "atlas"]
    assert out[0].fact_count == 3
    assert out[0].latest_fact_at == "2026-04-30T12:00:00+00:00"
    assert out[1].latest_fact_at is None


# ---- get_entity_with_facts ----


async def test_get_entity_with_facts_filters_superseded_by_default():
    import datetime as dt
    import uuid as _uuid

    ent = SimpleNamespace(
        id=_uuid.UUID("11111111-1111-1111-1111-111111111111"),
        name="auto-agent",
        entity_type="project",
        tags=["agent"],
    )
    f1 = SimpleNamespace(
        id=_uuid.UUID("22222222-2222-2222-2222-222222222222"),
        entity_id=ent.id,
        content="current",
        kind="fact",
        source="memory-tab",
        author="alan",
        valid_from=dt.datetime(2026, 4, 30, 12, 0, tzinfo=dt.UTC),
        valid_until=None,
    )

    captured = {}

    def execute_side_effect(stmt):
        # First call resolves the entity, subsequent calls return facts.
        # We don't actually inspect SQL — just dispatch in order.
        captured.setdefault("calls", 0)
        captured["calls"] += 1
        if captured["calls"] == 1:
            res = MagicMock()
            res.scalar_one_or_none.return_value = ent
            return res
        res = MagicMock()
        scalars = MagicMock()
        scalars.__iter__ = lambda self: iter([f1])
        res.scalars.return_value = scalars
        return res

    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute_side_effect)
    with patch("shared.memory_io.team_memory_session", return_value=_SessionCtx(session)):
        out = await get_entity_with_facts("auto-agent")
    assert out is not None
    assert out.entity.name == "auto-agent"
    assert len(out.facts) == 1
    assert out.facts[0].content == "current"


async def test_get_entity_with_facts_returns_none_when_missing():
    session = MagicMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=res)
    with patch("shared.memory_io.team_memory_session", return_value=_SessionCtx(session)):
        out = await get_entity_with_facts("missing")
    assert out is None


# ---- delete_fact ----


async def test_delete_fact_marks_valid_until():
    import datetime as dt
    import uuid as _uuid

    fact_id = "22222222-2222-2222-2222-222222222222"
    fact = SimpleNamespace(
        id=_uuid.UUID(fact_id),
        valid_until=None,
        source="memory-tab",
    )
    res = MagicMock()
    res.scalar_one_or_none.return_value = fact
    session = MagicMock()
    session.execute = AsyncMock(return_value=res)
    session.commit = AsyncMock()
    with patch("shared.memory_io.team_memory_session", return_value=_SessionCtx(session)):
        ok = await delete_fact(fact_id, author="alan")
    assert ok is True
    assert isinstance(fact.valid_until, dt.datetime)
    # Audit trail: deleter is appended to the existing source, original is preserved.
    assert "alan" in fact.source
    assert "memory-tab" in fact.source
    session.commit.assert_awaited_once()


async def test_delete_fact_records_deleter_when_source_was_empty():
    import uuid as _uuid

    fact = SimpleNamespace(
        id=_uuid.UUID("22222222-2222-2222-2222-222222222222"),
        valid_until=None,
        source=None,
    )
    res = MagicMock()
    res.scalar_one_or_none.return_value = fact
    session = MagicMock()
    session.execute = AsyncMock(return_value=res)
    session.commit = AsyncMock()
    with patch("shared.memory_io.team_memory_session", return_value=_SessionCtx(session)):
        await delete_fact("22222222-2222-2222-2222-222222222222", author="alan")
    assert "alan" in fact.source


async def test_delete_fact_returns_false_when_unknown():
    res = MagicMock()
    res.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=res)
    session.commit = AsyncMock()
    with patch("shared.memory_io.team_memory_session", return_value=_SessionCtx(session)):
        ok = await delete_fact("33333333-3333-3333-3333-333333333333")
    assert ok is False
    session.commit.assert_not_awaited()


async def test_delete_fact_rejects_non_uuid():
    ok = await delete_fact("not-a-uuid")
    assert ok is False
