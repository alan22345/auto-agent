import time
from unittest.mock import AsyncMock, patch

import pytest

from shared.types import (
    ConflictInfo,
    MemoryEntityDetail,
    MemoryEntitySummary,
    MemoryFact,
    ProposedFact,
)
from web.main import (
    MEMORY_SESSION_TTL_SEC,
    MemorySession,
    _handle_memory_correct_fact,
    _handle_memory_delete_fact,
    _handle_memory_extract,
    _handle_memory_get_entity,
    _handle_memory_reextract,
    _handle_memory_save,
    _handle_memory_search,
    _sweep_memory_sessions_once,
    memory_sessions,
)


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload: dict):
        self.sent.append(payload)


@pytest.fixture(autouse=True)
def _clear():
    memory_sessions.clear()
    yield
    memory_sessions.clear()


def _fact(entity="e", kind="fact", content="c", conflicts=None, resolution=None):
    return ProposedFact(
        row_id=f"r-{content[:4]}",
        entity=entity,
        entity_type="concept",
        kind=kind,
        content=content,
        conflicts=conflicts or [],
        resolution=resolution,
    ).model_dump()


async def test_extract_from_pasted_text():
    ws = FakeWS()
    with (
        patch(
            "web.main.extract",
            new=AsyncMock(
                return_value=[
                    ProposedFact(
                        row_id="r1", entity="e", entity_type="concept", kind="fact", content="c"
                    )
                ]
            ),
        ),
        patch("web.main.recall_entity", new=AsyncMock(return_value=None)),
    ):
        await _handle_memory_extract(
            ws,
            {
                "type": "memory_extract",
                "pasted_text": "hello world",
                "context_hint": "from standup",
            },
        )
    assert ws.sent[-1]["type"] == "memory_rows"
    assert ws.sent[-1]["rows"][0]["entity"] == "e"
    assert ws.sent[-1]["rows"][0]["entity_status"] == "new"


async def test_extract_from_source_id():
    memory_sessions["src-1"] = MemorySession(text="body text", user_id=1)
    ws = FakeWS()
    with (
        patch("web.main.extract", new=AsyncMock(return_value=[])) as ex,
        patch("web.main.recall_entity", new=AsyncMock(return_value=None)),
    ):
        await _handle_memory_extract(
            ws,
            {
                "type": "memory_extract",
                "source_id": "src-1",
            },
            user_id=1,
        )
    assert ex.await_args.kwargs["text"] == "body text"
    assert ws.sent[-1]["type"] == "memory_rows"


async def test_extract_rejects_both_inputs():
    ws = FakeWS()
    await _handle_memory_extract(
        ws,
        {
            "type": "memory_extract",
            "source_id": "x",
            "pasted_text": "y",
        },
    )
    assert ws.sent[-1]["type"] == "memory_error"


async def test_extract_rejects_neither():
    ws = FakeWS()
    await _handle_memory_extract(ws, {"type": "memory_extract"})
    assert ws.sent[-1]["type"] == "memory_error"


async def test_extract_tags_existing_entity():
    ws = FakeWS()
    with (
        patch(
            "web.main.extract",
            new=AsyncMock(
                return_value=[
                    ProposedFact(
                        row_id="r1",
                        entity="auto-agent",
                        entity_type="project",
                        kind="fact",
                        content="c",
                    )
                ]
            ),
        ),
        patch(
            "web.main.recall_entity",
            new=AsyncMock(
                return_value={
                    "entity": {"name": "auto-agent", "type": "project"},
                    "facts": [{"id": "f1", "content": "old", "kind": "fact"}],
                    "score": 0.95,
                }
            ),
        ),
    ):
        await _handle_memory_extract(
            ws,
            {
                "type": "memory_extract",
                "pasted_text": "x",
            },
        )
    row = ws.sent[-1]["rows"][0]
    assert row["entity_status"] == "exists"
    assert row["entity_match_score"] == 0.95


async def test_save_with_no_conflicts_calls_remember():
    ws = FakeWS()
    with (
        patch("web.main.remember_row", new=AsyncMock(return_value="f-new")) as rr,
        patch("web.main.correct_fact", new=AsyncMock()) as cf,
    ):
        await _handle_memory_save(
            ws,
            {
                "type": "memory_save",
                "rows": [_fact(content="one"), _fact(content="two")],
            },
        )
    assert rr.await_count == 2
    cf.assert_not_awaited()
    results = ws.sent[-1]["results"]
    assert all(r["ok"] for r in results)


async def test_save_rejects_unresolved_conflict():
    ws = FakeWS()
    row = _fact(
        content="x", conflicts=[ConflictInfo(fact_id="f1", existing_content="old").model_dump()]
    )
    with (
        patch("web.main.remember_row", new=AsyncMock()) as rr,
        patch("web.main.correct_fact", new=AsyncMock()) as cf,
    ):
        await _handle_memory_save(ws, {"type": "memory_save", "rows": [row]})
    assert ws.sent[-1]["type"] == "memory_error"
    rr.assert_not_awaited()
    cf.assert_not_awaited()


async def test_save_replace_routes_to_correct():
    ws = FakeWS()
    row = _fact(
        content="new",
        conflicts=[ConflictInfo(fact_id="f1", existing_content="old").model_dump()],
        resolution="replace",
    )
    with (
        patch("web.main.remember_row", new=AsyncMock()) as rr,
        patch("web.main.correct_fact", new=AsyncMock(return_value="f-upd")) as cf,
    ):
        await _handle_memory_save(ws, {"type": "memory_save", "rows": [row]})
    cf.assert_awaited_once()
    rr.assert_not_awaited()


async def test_save_keep_existing_skips():
    ws = FakeWS()
    row = _fact(
        content="new",
        conflicts=[ConflictInfo(fact_id="f1", existing_content="old").model_dump()],
        resolution="keep_existing",
    )
    with (
        patch("web.main.remember_row", new=AsyncMock()) as rr,
        patch("web.main.correct_fact", new=AsyncMock()) as cf,
    ):
        await _handle_memory_save(ws, {"type": "memory_save", "rows": [row]})
    rr.assert_not_awaited()
    cf.assert_not_awaited()
    assert ws.sent[-1]["results"][0]["ok"] is True


async def test_save_keep_both_calls_remember():
    ws = FakeWS()
    row = _fact(
        content="new",
        conflicts=[ConflictInfo(fact_id="f1", existing_content="old").model_dump()],
        resolution="keep_both",
    )
    with (
        patch("web.main.remember_row", new=AsyncMock(return_value="f-new")) as rr,
        patch("web.main.correct_fact", new=AsyncMock()) as cf,
    ):
        await _handle_memory_save(ws, {"type": "memory_save", "rows": [row]})
    rr.assert_awaited_once()
    cf.assert_not_awaited()


async def test_save_partial_failure():
    ws = FakeWS()
    with patch("web.main.remember_row", new=AsyncMock(side_effect=[Exception("db down"), "f-2"])):
        await _handle_memory_save(
            ws,
            {
                "type": "memory_save",
                "rows": [_fact(content="a"), _fact(content="b")],
            },
        )
    results = ws.sent[-1]["results"]
    assert results[0]["ok"] is False
    assert "db down" in results[0]["error"]
    assert results[1]["ok"] is True


async def test_reextract_uses_stored_text():
    memory_sessions["src-1"] = MemorySession(text="orig", user_id=1)
    ws = FakeWS()
    with (
        patch("web.main.extract", new=AsyncMock(return_value=[])) as ex,
        patch("web.main.recall_entity", new=AsyncMock(return_value=None)),
    ):
        await _handle_memory_reextract(
            ws,
            {
                "type": "memory_reextract",
                "source_id": "src-1",
                "note": "these are about X",
            },
            user_id=1,
        )
    assert ex.await_args.kwargs["text"] == "orig"
    assert "X" in ex.await_args.kwargs["hint"]


async def test_oversize_paste_rejected():
    ws = FakeWS()
    await _handle_memory_extract(
        ws,
        {
            "type": "memory_extract",
            "pasted_text": "x" * 250_001,
        },
    )
    assert ws.sent[-1]["type"] == "memory_error"
    assert "too large" in ws.sent[-1]["message"].lower()


async def test_extract_rejects_other_user_source_id():
    """A session owned by user 2 must not be accessible by user 1."""
    memory_sessions["src-other"] = MemorySession(text="secret", user_id=2)
    ws = FakeWS()
    await _handle_memory_extract(
        ws,
        {
            "type": "memory_extract",
            "source_id": "src-other",
        },
        user_id=1,
    )
    assert ws.sent[-1]["type"] == "memory_error"
    assert "access denied" in ws.sent[-1]["message"].lower()


# ---- Memory browser (read-side) handlers ----


async def test_search_with_query_calls_search_entities():
    ws = FakeWS()
    summary = MemoryEntitySummary(id="e1", name="auto-agent", type="project", tags=["a"])
    with (
        patch("web.main.search_entities", new=AsyncMock(return_value=[summary])) as se,
        patch("web.main.list_recent_entities", new=AsyncMock()) as lr,
    ):
        await _handle_memory_search(ws, {"type": "memory_search", "query": "auto"})
    se.assert_awaited_once()
    lr.assert_not_awaited()
    assert ws.sent[-1]["type"] == "memory_search_results"
    assert ws.sent[-1]["query"] == "auto"
    assert ws.sent[-1]["entities"][0]["name"] == "auto-agent"


async def test_search_blank_query_lists_recent():
    ws = FakeWS()
    with (
        patch("web.main.search_entities", new=AsyncMock()) as se,
        patch("web.main.list_recent_entities", new=AsyncMock(return_value=[])) as lr,
    ):
        await _handle_memory_search(ws, {"type": "memory_search", "query": "   "})
    lr.assert_awaited_once()
    se.assert_not_awaited()
    assert ws.sent[-1]["type"] == "memory_search_results"
    assert ws.sent[-1]["entities"] == []


async def test_get_entity_returns_detail():
    ws = FakeWS()
    detail = MemoryEntityDetail(
        entity=MemoryEntitySummary(id="e1", name="auto-agent", type="project"),
        facts=[MemoryFact(id="f1", content="x", kind="fact")],
    )
    with patch("web.main.get_entity_with_facts", new=AsyncMock(return_value=detail)) as g:
        await _handle_memory_get_entity(ws, {"type": "memory_get_entity", "entity": "auto-agent"})
    g.assert_awaited_once()
    assert g.await_args.kwargs["include_superseded"] is False
    assert ws.sent[-1]["type"] == "memory_entity"
    assert ws.sent[-1]["detail"]["entity"]["name"] == "auto-agent"
    assert ws.sent[-1]["detail"]["facts"][0]["id"] == "f1"


async def test_get_entity_passes_include_superseded():
    ws = FakeWS()
    detail = MemoryEntityDetail(
        entity=MemoryEntitySummary(id="e1", name="x", type="project"), facts=[]
    )
    with patch("web.main.get_entity_with_facts", new=AsyncMock(return_value=detail)) as g:
        await _handle_memory_get_entity(
            ws,
            {
                "type": "memory_get_entity",
                "entity": "x",
                "include_superseded": True,
            },
        )
    assert g.await_args.kwargs["include_superseded"] is True
    assert ws.sent[-1]["include_superseded"] is True


async def test_get_entity_missing_name_errors():
    ws = FakeWS()
    await _handle_memory_get_entity(ws, {"type": "memory_get_entity", "entity": ""})
    assert ws.sent[-1]["type"] == "memory_error"


async def test_get_entity_not_found_errors():
    ws = FakeWS()
    with patch("web.main.get_entity_with_facts", new=AsyncMock(return_value=None)):
        await _handle_memory_get_entity(ws, {"type": "memory_get_entity", "entity": "nope"})
    assert ws.sent[-1]["type"] == "memory_error"
    assert "not found" in ws.sent[-1]["message"].lower()


async def test_correct_fact_routes_to_correct_fact():
    ws = FakeWS()
    with patch("web.main.correct_fact", new=AsyncMock(return_value="f-new")) as cf:
        await _handle_memory_correct_fact(
            ws,
            {
                "type": "memory_correct_fact",
                "fact_id": "f1",
                "content": "updated",
            },
            username="alan",
        )
    cf.assert_awaited_once()
    kwargs = cf.await_args.kwargs
    assert kwargs["author"] == "alan"
    assert kwargs["reason"] is None
    assert ws.sent[-1]["type"] == "memory_fact_corrected"
    assert ws.sent[-1]["new_fact_id"] == "f-new"


async def test_correct_fact_forwards_reason():
    ws = FakeWS()
    with patch("web.main.correct_fact", new=AsyncMock(return_value="f-new")) as cf:
        await _handle_memory_correct_fact(
            ws,
            {
                "type": "memory_correct_fact",
                "fact_id": "f1",
                "content": "updated",
                "reason": "ratified at standup",
            },
            username="alan",
        )
    assert cf.await_args.kwargs["reason"] == "ratified at standup"


async def test_correct_fact_missing_id_errors():
    ws = FakeWS()
    await _handle_memory_correct_fact(
        ws,
        {
            "type": "memory_correct_fact",
            "fact_id": "",
            "content": "x",
        },
    )
    assert ws.sent[-1]["type"] == "memory_error"


async def test_correct_fact_missing_content_errors():
    ws = FakeWS()
    await _handle_memory_correct_fact(
        ws,
        {
            "type": "memory_correct_fact",
            "fact_id": "f1",
            "content": "  ",
        },
    )
    assert ws.sent[-1]["type"] == "memory_error"


async def test_correct_fact_engine_failure_emits_error():
    ws = FakeWS()
    with patch("web.main.correct_fact", new=AsyncMock(side_effect=Exception("db down"))):
        await _handle_memory_correct_fact(
            ws,
            {
                "type": "memory_correct_fact",
                "fact_id": "f1",
                "content": "x",
            },
        )
    assert ws.sent[-1]["type"] == "memory_error"
    assert "db down" in ws.sent[-1]["message"]


async def test_delete_fact_happy_path():
    ws = FakeWS()
    with patch("web.main.delete_fact", new=AsyncMock(return_value=True)) as df:
        await _handle_memory_delete_fact(
            ws,
            {
                "type": "memory_delete_fact",
                "fact_id": "f1",
            },
            username="alan",
        )
    df.assert_awaited_once()
    assert df.await_args.kwargs["author"] == "alan"
    assert ws.sent[-1]["type"] == "memory_fact_deleted"
    assert ws.sent[-1]["fact_id"] == "f1"


async def test_delete_fact_missing_id_errors():
    ws = FakeWS()
    await _handle_memory_delete_fact(ws, {"type": "memory_delete_fact", "fact_id": ""})
    assert ws.sent[-1]["type"] == "memory_error"


async def test_delete_fact_unknown_id_errors():
    ws = FakeWS()
    with patch("web.main.delete_fact", new=AsyncMock(return_value=False)):
        await _handle_memory_delete_fact(
            ws,
            {
                "type": "memory_delete_fact",
                "fact_id": "f-missing",
            },
        )
    assert ws.sent[-1]["type"] == "memory_error"
    assert "unknown" in ws.sent[-1]["message"].lower()


def test_sessions_sweeper_drops_stale():
    """Sessions older than TTL must be removed by the sweeper."""
    memory_sessions["old-1"] = MemorySession(
        text="stale", user_id=1, created_at=time.time() - MEMORY_SESSION_TTL_SEC - 1
    )
    memory_sessions["new-1"] = MemorySession(text="fresh", user_id=1)
    _sweep_memory_sessions_once()
    assert "old-1" not in memory_sessions
    assert "new-1" in memory_sessions
