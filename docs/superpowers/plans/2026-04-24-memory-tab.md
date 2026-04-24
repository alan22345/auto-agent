# Memory Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Memory" tab to the auto-agent web UI that lets teammates drop a file, paste text, or upload a PDF, then have an LLM extract structured facts that they review, correct, and save into the shared team-memory graph.

**Architecture:** Single-page extension of `web/static/index.html` (vanilla JS, no framework). A new FastAPI HTTP endpoint handles uploads (parses to text, discards bytes). Three new websocket handlers in `web/main.py` drive extract/re-extract/save. A new `agent/memory_extractor.py` makes one structured-output LLM call per extract and flags conflicts with existing facts. Writes go through the existing `team_memory.graph.GraphEngine` via `shared.database.team_memory_session`.

**Tech Stack:** FastAPI, vanilla JS, `team_memory.graph.GraphEngine`, `agent.llm.get_provider`, `pypdf` (new dep).

**Spec:** `docs/superpowers/specs/2026-04-24-memory-tab-design.md`

---

## Pre-flight context the engineer needs

- Layering (see `CLAUDE.md`): `shared/` → `agent/` → `web/`. Never import upward.
- Existing team-memory usage example: `agent/context/memory.py` — uses `team_memory_session()` + `GraphEngine.recall(query=...)`. `recall` returns `{"matches": [{"entity": {...}, "facts": [...]}], "ambiguous": bool}`. This is the pattern to copy.
- LLM access: `agent.llm.get_provider()` returns a provider with `.complete(messages, system=..., max_tokens=..., temperature=...)` → `LLMResponse` with `.content` (list of blocks; first text block is what we want). No tools needed.
- WS handler pattern: in `web/main.py`, the dispatcher at ~line 95 (`msg_type = data.get("type", ...)`) branches into `_handle_*` coroutines. Follow that pattern exactly.
- Frontend pattern: `web/static/index.html` — tabs are `<button>` elements inside `.sidebar-tabs` at ~line 836, switched by `switchTab()` at ~line 987. Main panels are siblings (`#tasks-main`, `#freeform-main`); toggle with the same `display: flex / none` pattern.
- Tests: `.venv/bin/python3 -m pytest tests/ -q`. Ruff: `ruff check .`.
- `GraphEngine.remember` requires an `entity_type` (e.g. `project`, `concept`, `person`). The extractor must produce it; UI exposes it as a dropdown on each row.
- `GraphEngine.correct(fact_id, new_content, reason)` — `reason` is required; we'll pass a fixed string like "updated via memory tab" for v1.

---

## File Structure

- **Create** `agent/memory_extractor.py` — stateless function `extract(text, hint, existing_facts_by_entity) -> list[ProposedFact]`. Runs one LLM call.
- **Create** `shared/memory_io.py` — thin wrapper around `GraphEngine`: `async def recall_entity(name)`, `async def remember_row(row)`, `async def correct_fact(fact_id, new_content)`. Keeps web handlers slim and unit-testable.
- **Create** `tests/test_memory_extractor.py` — unit tests with a mocked LLM provider.
- **Create** `tests/test_memory_ws_handlers.py` — unit tests for the three websocket handlers with a fake `GraphEngine`.
- **Create** `tests/test_memory_upload.py` — unit tests for the upload endpoint (text + tiny PDF + oversize).
- **Create** `tests/fixtures/tiny.pdf` — 1-page PDF fixture (generated in a test, not checked in binary).
- **Modify** `web/main.py` — add `POST /memory/upload`, three `_handle_memory_*` handlers, wire into the dispatcher, add `memory_sessions: dict[str, MemorySession]` module-level store keyed by websocket id.
- **Modify** `web/static/index.html` — new "Memory" sidebar tab button, `#memory-panel` sidebar pane, `#memory-main` main pane with drop zone, textarea, review table, conflict-row expansion, and `switchTab('memory')` wiring.
- **Modify** `pyproject.toml` — add `pypdf` dependency.
- **Modify** `shared/types.py` — add `ProposedFact`, `ConflictInfo`, `MemorySaveResult` Pydantic models.

---

## Task 1: Add pypdf dependency and a PDF fixture helper

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/pdf_fixture.py`

- [ ] **Step 1: Add pypdf to dependencies**

Edit `pyproject.toml`. Find the `[project]` dependencies list and append `"pypdf>=4.0"` in alphabetical position. Example addition:

```toml
    "pypdf>=4.0",
```

- [ ] **Step 2: Install**

Run: `.venv/bin/pip install -e .`
Expected: `Successfully installed pypdf-...`

- [ ] **Step 3: Create a runtime PDF fixture helper**

Create `tests/fixtures/__init__.py` (empty file) and `tests/fixtures/pdf_fixture.py`:

```python
"""Build a tiny in-memory PDF for tests. Avoids checking a binary into git."""
from __future__ import annotations

from io import BytesIO


def make_pdf_bytes(text: str = "hello from a tiny pdf") -> bytes:
    """Return a valid 1-page PDF containing the given text."""
    from pypdf import PdfWriter
    from pypdf.generic import ContentStream, NameObject, RectangleObject

    # Minimal valid PDF: pypdf's PdfWriter can't create content from scratch easily,
    # so we build the smallest hand-rolled PDF that pypdf can read back.
    content = (
        "BT /F1 12 Tf 72 720 Td (" + text.replace("(", r"\(").replace(")", r"\)") + ") Tj ET"
    )
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length " + str(len(content)).encode() + b">>stream\n"
        + content.encode() + b"\nendstream\nendobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    )
    xref_offset = len(body)
    xref = (
        b"xref\n0 6\n0000000000 65535 f \n"
        + b"".join(
            f"{body.find(f'{i} 0 obj'.encode()):010d} 00000 n \n".encode()
            for i in range(1, 6)
        )
    )
    trailer = b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n" + str(xref_offset).encode() + b"\n%%EOF"
    return body + xref + trailer
```

- [ ] **Step 4: Smoke-test the fixture**

Run:
```bash
.venv/bin/python3 -c "
from pypdf import PdfReader
from io import BytesIO
from tests.fixtures.pdf_fixture import make_pdf_bytes
r = PdfReader(BytesIO(make_pdf_bytes('round trip text')))
print(r.pages[0].extract_text())
"
```
Expected: prints `round trip text` (or text containing that phrase). If pypdf can't parse it, fix the fixture builder before moving on.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/fixtures/
git commit -m "feat(memory-tab): add pypdf dep and tiny-pdf test fixture"
```

---

## Task 2: Add Pydantic types

**Files:**
- Modify: `shared/types.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_types.py` (create file if absent):

```python
from shared.types import ProposedFact, ConflictInfo, MemorySaveResult


def test_proposed_fact_defaults():
    pf = ProposedFact(
        row_id="r1", entity="auto-agent", entity_type="project",
        kind="fact", content="hello",
    )
    assert pf.conflicts == []
    assert pf.entity_status == "new"
    assert pf.resolution is None


def test_conflict_info_roundtrip():
    c = ConflictInfo(fact_id="f1", existing_content="old")
    assert c.fact_id == "f1"


def test_memory_save_result_counts():
    r = MemorySaveResult(row_id="r1", ok=True)
    assert r.ok is True
    assert r.error is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_types.py -v`
Expected: FAIL — `ImportError: cannot import name 'ProposedFact'`.

- [ ] **Step 3: Implement the types**

Append to `shared/types.py`:

```python
from typing import Literal


KindLiteral = Literal["decision", "architecture", "gotcha", "status", "preference", "fact"]
EntityStatus = Literal["new", "exists"]
Resolution = Literal["keep_existing", "replace", "keep_both"]


class ConflictInfo(BaseModel):
    fact_id: str
    existing_content: str


class ProposedFact(BaseModel):
    row_id: str
    entity: str
    entity_type: str = "concept"
    entity_status: EntityStatus = "new"
    entity_match_score: float | None = None
    kind: KindLiteral = "fact"
    content: str
    conflicts: list[ConflictInfo] = Field(default_factory=list)
    resolution: Resolution | None = None  # required on save iff conflicts


class MemorySaveResult(BaseModel):
    row_id: str
    ok: bool
    error: str | None = None
    fact_id: str | None = None
```

If `BaseModel` / `Field` aren't already imported at the top of `shared/types.py`, add `from pydantic import BaseModel, Field`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_types.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/types.py tests/test_types.py
git commit -m "feat(memory-tab): add ProposedFact/ConflictInfo/MemorySaveResult types"
```

---

## Task 3: memory_io wrapper around GraphEngine

**Files:**
- Create: `shared/memory_io.py`
- Create: `tests/test_memory_io.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_io.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from shared.memory_io import recall_entity, remember_row, correct_fact
from shared.types import ProposedFact, ConflictInfo


class FakeEngine:
    def __init__(self, recall_result=None, remember_result=None, correct_result=None):
        self.recall = AsyncMock(return_value=recall_result or {"matches": [], "ambiguous": False})
        self.remember = AsyncMock(return_value=remember_result or {"fact_id": "f-new"})
        self.correct = AsyncMock(return_value=correct_result or {"fact_id": "f-upd"})


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

    class _Ctx:
        async def __aenter__(self_inner): return "session"
        async def __aexit__(self_inner, *a): return False

    with patch("shared.memory_io.team_memory_session", return_value=_Ctx()), \
         patch("shared.memory_io.GraphEngine", return_value=fake):
        yield fake


async def test_recall_entity_returns_match(patched_session):
    result = await recall_entity("auto-agent")
    assert result["entity"]["name"] == "auto-agent"
    assert result["facts"][0]["id"] == "f1"
    assert result["score"] == 0.9


async def test_recall_entity_no_match():
    class _Ctx:
        async def __aenter__(self_inner): return "session"
        async def __aexit__(self_inner, *a): return False
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
```

Also ensure `tests/conftest.py` has `asyncio_mode = "auto"` or that async tests are decorated with `@pytest.mark.asyncio`. Check the existing `pyproject.toml` / `pytest.ini` for the current convention — if tests in this repo already use plain `async def test_...` without decoration, pytest-asyncio is in auto mode and no change is needed. If not, add `@pytest.mark.asyncio` to each async test above.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_memory_io.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.memory_io'`.

- [ ] **Step 3: Implement**

Create `shared/memory_io.py`:

```python
"""Thin async wrapper around team_memory.graph.GraphEngine.

Keeps web handlers slim and gives us a single seam to mock in tests.
"""
from __future__ import annotations

from typing import Any

import structlog
from team_memory.graph import GraphEngine

from shared.database import team_memory_session
from shared.types import ProposedFact

logger = structlog.get_logger()


async def recall_entity(name: str) -> dict[str, Any] | None:
    """Return the top match for an entity name, or None.

    Shape: {"entity": {...}, "facts": [...], "score": float}
    """
    if team_memory_session is None:
        return None
    try:
        async with team_memory_session() as session:
            engine = GraphEngine(session)
            result = await engine.recall(query=name)
    except Exception as e:
        logger.warning("memory_recall_failed", name=name, error=str(e))
        return None
    matches = result.get("matches") or []
    if not matches:
        return None
    return matches[0]


async def remember_row(row: ProposedFact, *, author: str | None = None) -> str:
    """Persist a new fact. Returns the new fact_id."""
    async with team_memory_session() as session:
        engine = GraphEngine(session)
        result = await engine.remember(
            content=row.content,
            entity=row.entity,
            entity_type=row.entity_type,
            kind=row.kind,
            source="memory-tab",
            author=author,
        )
    return result.get("fact_id", "")


async def correct_fact(fact_id: str, new_content: str, *, author: str | None = None) -> str:
    """Supersede an existing fact with new content via the correct flow."""
    async with team_memory_session() as session:
        engine = GraphEngine(session)
        result = await engine.correct(
            fact_id=fact_id,
            new_content=new_content,
            reason="updated via memory tab",
            source="memory-tab",
            author=author,
        )
    return result.get("fact_id", "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_memory_io.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add shared/memory_io.py tests/test_memory_io.py
git commit -m "feat(memory-tab): shared memory_io wrapper for GraphEngine"
```

---

## Task 4: MemoryExtractor — LLM call that returns proposed facts

**Files:**
- Create: `agent/memory_extractor.py`
- Create: `tests/test_memory_extractor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_extractor.py`:

```python
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.memory_extractor import extract
from shared.types import ProposedFact


def _mock_provider(text_response: str):
    provider = MagicMock()
    response = MagicMock()
    # LLMResponse has .content as list of blocks; first text block
    block = MagicMock()
    block.type = "text"
    block.text = text_response
    response.content = [block]
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
    # row_ids are unique
    assert len({r.row_id for r in rows}) == 2


async def test_extract_fallback_kind_when_missing():
    payload = json.dumps({"facts": [
        {"entity": "e", "entity_type": "concept", "content": "no kind here"}
    ]})
    rows = await extract("t", None, {}, _mock_provider(payload))
    assert rows[0].kind == "fact"


async def test_extract_retries_on_bad_json():
    provider = MagicMock()
    bad = MagicMock(); bad.type = "text"; bad.text = "not json at all"
    good = MagicMock(); good.type = "text"; good.text = json.dumps({"facts": [
        {"entity": "e", "entity_type": "concept", "kind": "fact", "content": "c"}
    ]})
    r_bad = MagicMock(); r_bad.content = [bad]
    r_good = MagicMock(); r_good.content = [good]
    provider.complete = AsyncMock(side_effect=[r_bad, r_good])
    rows = await extract("t", None, {}, provider)
    assert len(rows) == 1
    assert provider.complete.await_count == 2


async def test_extract_raises_after_two_bad_attempts():
    provider = MagicMock()
    bad = MagicMock(); bad.type = "text"; bad.text = "still not json"
    r = MagicMock(); r.content = [bad]
    provider.complete = AsyncMock(return_value=r)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_memory_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.memory_extractor'`.

- [ ] **Step 3: Implement**

Create `agent/memory_extractor.py`:

```python
"""Single-call LLM extractor: source text → proposed team-memory facts."""
from __future__ import annotations

import json
import uuid
from typing import Any

import structlog

from agent.llm import get_provider
from agent.llm.types import Message
from shared.types import ConflictInfo, ProposedFact

logger = structlog.get_logger()

_ALLOWED_KINDS = {"decision", "architecture", "gotcha", "status", "preference", "fact"}

_SYSTEM_PROMPT = """You extract structured facts for a team-memory knowledge graph.

Given source text, return STRICT JSON with this exact shape (no prose, no code fences):

{"facts": [
  {"entity": "<name>", "entity_type": "<project|concept|person|repo|system>",
   "kind": "<decision|architecture|gotcha|status|preference|fact>",
   "content": "<one concise fact, 1-2 sentences>",
   "conflicts_with": ["<existing_fact_id>", ...]  // OPTIONAL, only when content directly contradicts an existing fact
  }
]}

Rules:
- Each fact must be a self-contained statement that makes sense without the source doc.
- Prefer concrete, load-bearing information: decisions with their why, gotchas with their symptom, statuses with their date.
- Do NOT repeat facts that already exist in the provided "Existing facts" section unless genuinely correcting them.
- Only set conflicts_with when the new content directly contradicts (not augments) an existing fact.
- If you cannot extract anything useful, return {"facts": []}.
"""


def _build_user_message(text: str, hint: str | None, existing: dict[str, list[dict]]) -> str:
    parts: list[str] = []
    if hint:
        parts.append(f"CONTEXT HINT: {hint}\n")
    if existing:
        parts.append("EXISTING FACTS (for conflict checking):")
        for entity, facts in existing.items():
            parts.append(f"- Entity: {entity}")
            for f in facts:
                parts.append(f"  - id={f['id']} kind={f.get('kind','?')}: {f['content']}")
        parts.append("")
    parts.append("SOURCE TEXT:")
    parts.append(text)
    return "\n".join(parts)


def _parse_response(raw: str) -> list[dict[str, Any]]:
    cleaned = raw.strip()
    # Strip common markdown fences defensively.
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.rsplit("```", 1)[0]
    data = json.loads(cleaned)
    facts = data.get("facts", [])
    if not isinstance(facts, list):
        raise ValueError("'facts' is not a list")
    return facts


def _to_proposed(raw: dict[str, Any], existing_by_id: dict[str, dict]) -> ProposedFact:
    kind = raw.get("kind", "fact")
    if kind not in _ALLOWED_KINDS:
        kind = "fact"
    conflicts: list[ConflictInfo] = []
    for fact_id in raw.get("conflicts_with") or []:
        existing = existing_by_id.get(fact_id)
        if existing:
            conflicts.append(ConflictInfo(fact_id=fact_id, existing_content=existing["content"]))
    return ProposedFact(
        row_id=f"r-{uuid.uuid4().hex[:8]}",
        entity=raw.get("entity", "").strip() or "unknown",
        entity_type=raw.get("entity_type", "concept"),
        kind=kind,
        content=raw.get("content", "").strip(),
        conflicts=conflicts,
    )


async def extract(
    text: str,
    hint: str | None,
    existing_facts_by_entity: dict[str, list[dict]],
    provider=None,
) -> list[ProposedFact]:
    """Run one structured-output LLM call and return proposed facts.

    Retries once on malformed JSON; raises ValueError after the second failure.
    """
    if provider is None:
        provider = get_provider()

    # Flatten existing facts for O(1) id lookup.
    existing_by_id: dict[str, dict] = {}
    for facts in existing_facts_by_entity.values():
        for f in facts:
            existing_by_id[f["id"]] = f

    user_message = _build_user_message(text, hint, existing_facts_by_entity)
    messages = [Message(role="user", content=user_message)]

    for attempt in (1, 2):
        system = _SYSTEM_PROMPT
        if attempt == 2:
            system += "\n\nYour previous response was not valid JSON. Return ONLY valid JSON now."
        response = await provider.complete(
            messages=messages,
            system=system,
            max_tokens=4096,
            temperature=0.0,
        )
        text_block = next((b for b in response.content if getattr(b, "type", None) == "text"), None)
        raw_text = (text_block.text if text_block else "") or ""
        try:
            facts_raw = _parse_response(raw_text)
            return [_to_proposed(f, existing_by_id) for f in facts_raw]
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("memory_extract_parse_failed", attempt=attempt, error=str(e))
            last_error = e

    raise ValueError(f"could not parse extractor response after 2 attempts: {last_error}")
```

Note on `Message` construction: verify the exact shape by opening `agent/llm/types.py`. If `Message` takes `content=` as a string, the above works. If it takes a list of blocks, wrap the string accordingly.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_memory_extractor.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/memory_extractor.py tests/test_memory_extractor.py
git commit -m "feat(memory-tab): MemoryExtractor with JSON retry and conflict flagging"
```

---

## Task 5: Upload endpoint — parse and discard

**Files:**
- Modify: `web/main.py` (new handler only; do not touch websocket yet)
- Create: `tests/test_memory_upload.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_upload.py`:

```python
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.pdf_fixture import make_pdf_bytes
from web.main import app, memory_sessions


@pytest.fixture(autouse=True)
def _clear_sessions():
    memory_sessions.clear()
    yield
    memory_sessions.clear()


def test_upload_text_file_returns_source_id():
    client = TestClient(app)
    resp = client.post(
        "/memory/upload",
        files={"file": ("notes.md", b"# Hello\n\nSome notes.", "text/markdown")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "source_id" in body
    assert body["char_count"] == len("# Hello\n\nSome notes.")
    # text is held in server-side session store
    assert memory_sessions[body["source_id"]].text.startswith("# Hello")


def test_upload_pdf_file_parsed():
    client = TestClient(app)
    resp = client.post(
        "/memory/upload",
        files={"file": ("doc.pdf", make_pdf_bytes("pdf payload text"), "application/pdf")},
    )
    assert resp.status_code == 200
    sid = resp.json()["source_id"]
    assert "pdf payload text" in memory_sessions[sid].text


def test_upload_oversize_rejected():
    client = TestClient(app)
    big = ("x" * 250_000).encode()
    resp = client.post("/memory/upload", files={"file": ("big.txt", big, "text/plain")})
    assert resp.status_code == 400
    assert "too large" in resp.text.lower()


def test_upload_unknown_extension_rejected():
    client = TestClient(app)
    resp = client.post(
        "/memory/upload",
        files={"file": ("weird.xyz", b"whatever", "application/octet-stream")},
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_memory_upload.py -v`
Expected: FAIL — `ImportError: cannot import name 'memory_sessions' from 'web.main'`.

- [ ] **Step 3: Implement the endpoint and session store**

Edit `web/main.py`. Near the top (after imports, before `broadcast`), add:

```python
from dataclasses import dataclass, field
from io import BytesIO
import uuid

MEMORY_MAX_CHARS = 200_000


@dataclass
class MemorySession:
    text: str
    char_count: int = 0

    def __post_init__(self) -> None:
        self.char_count = len(self.text)


# keyed by source_id; cleared on save or websocket disconnect
memory_sessions: dict[str, MemorySession] = {}
```

Then add the endpoint (can sit next to the existing `@app.get("/")`):

```python
from fastapi import UploadFile, File, HTTPException


@app.post("/memory/upload")
async def memory_upload(file: UploadFile = File(...)) -> dict:
    """Parse an uploaded file to text, hold the text on the server, discard bytes."""
    name = (file.filename or "").lower()
    if name.endswith(".pdf"):
        raw = await file.read()
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(raw))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"could not parse pdf: {e}")
        finally:
            del raw  # release bytes immediately
    elif name.endswith((".txt", ".md", ".log")):
        raw = await file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(status_code=400, detail=f"not utf-8: {e}")
        finally:
            del raw
    else:
        raise HTTPException(status_code=400, detail="only .txt, .md, .log, .pdf are supported")

    if len(text) > MEMORY_MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"file too large: {len(text)} chars (cap {MEMORY_MAX_CHARS})")

    source_id = f"src-{uuid.uuid4().hex[:12]}"
    memory_sessions[source_id] = MemorySession(text=text)
    return {"source_id": source_id, "char_count": len(text)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_memory_upload.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint**

Run: `ruff check web/main.py tests/test_memory_upload.py`
Expected: no errors. Fix any issues.

- [ ] **Step 6: Commit**

```bash
git add web/main.py tests/test_memory_upload.py
git commit -m "feat(memory-tab): POST /memory/upload with pdf/text parsing and byte discard"
```

---

## Task 6: Websocket handlers — extract / reextract / save

**Files:**
- Modify: `web/main.py`
- Create: `tests/test_memory_ws_handlers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_ws_handlers.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from web.main import (
    MemorySession,
    _handle_memory_extract,
    _handle_memory_reextract,
    _handle_memory_save,
    memory_sessions,
)
from shared.types import ProposedFact, ConflictInfo


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
        entity=entity, entity_type="concept", kind=kind,
        content=content, conflicts=conflicts or [], resolution=resolution,
    ).model_dump()


async def test_extract_from_pasted_text():
    ws = FakeWS()
    with patch("web.main.extract", new=AsyncMock(return_value=[
        ProposedFact(row_id="r1", entity="e", entity_type="concept", kind="fact", content="c")
    ])), patch("web.main.recall_entity", new=AsyncMock(return_value=None)):
        await _handle_memory_extract(ws, {
            "type": "memory_extract",
            "pasted_text": "hello world",
            "context_hint": "from standup",
        })
    assert ws.sent[-1]["type"] == "memory_rows"
    assert ws.sent[-1]["rows"][0]["entity"] == "e"
    assert ws.sent[-1]["rows"][0]["entity_status"] == "new"


async def test_extract_from_source_id():
    memory_sessions["src-1"] = MemorySession(text="body text")
    ws = FakeWS()
    with patch("web.main.extract", new=AsyncMock(return_value=[])) as ex, \
         patch("web.main.recall_entity", new=AsyncMock(return_value=None)):
        await _handle_memory_extract(ws, {
            "type": "memory_extract", "source_id": "src-1",
        })
    assert ex.await_args.kwargs["text"] == "body text"
    assert ws.sent[-1]["type"] == "memory_rows"


async def test_extract_rejects_both_inputs():
    ws = FakeWS()
    await _handle_memory_extract(ws, {
        "type": "memory_extract", "source_id": "x", "pasted_text": "y",
    })
    assert ws.sent[-1]["type"] == "memory_error"


async def test_extract_rejects_neither():
    ws = FakeWS()
    await _handle_memory_extract(ws, {"type": "memory_extract"})
    assert ws.sent[-1]["type"] == "memory_error"


async def test_extract_tags_existing_entity():
    ws = FakeWS()
    with patch("web.main.extract", new=AsyncMock(return_value=[
        ProposedFact(row_id="r1", entity="auto-agent", entity_type="project",
                     kind="fact", content="c")
    ])), patch("web.main.recall_entity", new=AsyncMock(return_value={
        "entity": {"name": "auto-agent", "type": "project"},
        "facts": [{"id": "f1", "content": "old", "kind": "fact"}],
        "score": 0.95,
    })):
        await _handle_memory_extract(ws, {
            "type": "memory_extract", "pasted_text": "x",
        })
    row = ws.sent[-1]["rows"][0]
    assert row["entity_status"] == "exists"
    assert row["entity_match_score"] == 0.95


async def test_save_with_no_conflicts_calls_remember():
    ws = FakeWS()
    with patch("web.main.remember_row", new=AsyncMock(return_value="f-new")) as rr, \
         patch("web.main.correct_fact", new=AsyncMock()) as cf:
        await _handle_memory_save(ws, {
            "type": "memory_save",
            "rows": [_fact(content="one"), _fact(content="two")],
        })
    assert rr.await_count == 2
    cf.assert_not_awaited()
    results = ws.sent[-1]["results"]
    assert all(r["ok"] for r in results)


async def test_save_rejects_unresolved_conflict():
    ws = FakeWS()
    row = _fact(content="x", conflicts=[ConflictInfo(fact_id="f1", existing_content="old").model_dump()])
    with patch("web.main.remember_row", new=AsyncMock()) as rr, \
         patch("web.main.correct_fact", new=AsyncMock()) as cf:
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
    with patch("web.main.remember_row", new=AsyncMock()) as rr, \
         patch("web.main.correct_fact", new=AsyncMock(return_value="f-upd")) as cf:
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
    with patch("web.main.remember_row", new=AsyncMock()) as rr, \
         patch("web.main.correct_fact", new=AsyncMock()) as cf:
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
    with patch("web.main.remember_row", new=AsyncMock(return_value="f-new")) as rr, \
         patch("web.main.correct_fact", new=AsyncMock()) as cf:
        await _handle_memory_save(ws, {"type": "memory_save", "rows": [row]})
    rr.assert_awaited_once()
    cf.assert_not_awaited()


async def test_save_partial_failure():
    ws = FakeWS()
    with patch("web.main.remember_row", new=AsyncMock(side_effect=[Exception("db down"), "f-2"])):
        await _handle_memory_save(ws, {
            "type": "memory_save",
            "rows": [_fact(content="a"), _fact(content="b")],
        })
    results = ws.sent[-1]["results"]
    assert results[0]["ok"] is False
    assert "db down" in results[0]["error"]
    assert results[1]["ok"] is True


async def test_reextract_uses_stored_text():
    memory_sessions["src-1"] = MemorySession(text="orig")
    ws = FakeWS()
    with patch("web.main.extract", new=AsyncMock(return_value=[])) as ex, \
         patch("web.main.recall_entity", new=AsyncMock(return_value=None)):
        await _handle_memory_reextract(ws, {
            "type": "memory_reextract", "source_id": "src-1",
            "note": "these are about X",
        })
    assert ex.await_args.kwargs["text"] == "orig"
    assert "X" in ex.await_args.kwargs["hint"]


async def test_oversize_paste_rejected():
    ws = FakeWS()
    await _handle_memory_extract(ws, {
        "type": "memory_extract", "pasted_text": "x" * 250_001,
    })
    assert ws.sent[-1]["type"] == "memory_error"
    assert "too large" in ws.sent[-1]["message"].lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_memory_ws_handlers.py -v`
Expected: FAIL — handlers not defined.

- [ ] **Step 3: Implement handlers**

Edit `web/main.py`. Add imports near the top:

```python
from agent.memory_extractor import extract
from shared.memory_io import recall_entity, remember_row, correct_fact
from shared.types import ProposedFact, MemorySaveResult, ConflictInfo
```

Add the three handlers (near other `_handle_*` functions):

```python
async def _send_memory_error(ws, message: str) -> None:
    await ws.send_json({"type": "memory_error", "message": message})


async def _run_memory_extract(
    ws, text: str, hint: str | None, source_id: str | None,
) -> None:
    """Shared core for extract + reextract."""
    if len(text) > MEMORY_MAX_CHARS:
        await _send_memory_error(ws, f"input too large: {len(text)} chars (cap {MEMORY_MAX_CHARS})")
        return

    # First-pass: extract with NO existing-facts context (we need entity names first).
    try:
        first_pass = await extract(text=text, hint=hint, existing_facts_by_entity={})
    except ValueError as e:
        await _send_memory_error(ws, f"extraction failed: {e}")
        return

    # Look up existing facts for each proposed entity.
    existing_by_entity: dict[str, list[dict]] = {}
    entity_match: dict[str, dict] = {}
    for row in first_pass:
        if row.entity in existing_by_entity:
            continue
        match = await recall_entity(row.entity)
        if match:
            entity_match[row.entity] = match
            existing_by_entity[row.entity] = match.get("facts", [])

    # Second pass ONLY if we found existing entities — lets the LLM tag conflicts.
    # If nothing matched, skip the extra call.
    if existing_by_entity:
        try:
            rows = await extract(text=text, hint=hint, existing_facts_by_entity=existing_by_entity)
        except ValueError as e:
            await _send_memory_error(ws, f"extraction failed: {e}")
            return
    else:
        rows = first_pass

    # Annotate each row with entity_status + score.
    for row in rows:
        if row.entity in entity_match:
            row.entity_status = "exists"
            row.entity_match_score = entity_match[row.entity].get("score")
        else:
            row.entity_status = "new"

    await ws.send_json({
        "type": "memory_rows",
        "source_id": source_id,
        "rows": [r.model_dump() for r in rows],
    })


async def _handle_memory_extract(ws, data: dict) -> None:
    source_id = data.get("source_id")
    pasted = data.get("pasted_text")
    hint = (data.get("context_hint") or "").strip() or None

    if bool(source_id) == bool(pasted):
        await _send_memory_error(ws, "provide exactly one of source_id or pasted_text")
        return

    if source_id:
        sess = memory_sessions.get(source_id)
        if not sess:
            await _send_memory_error(ws, f"unknown source_id: {source_id}")
            return
        text = sess.text
    else:
        text = pasted

    await _run_memory_extract(ws, text=text, hint=hint, source_id=source_id)


async def _handle_memory_reextract(ws, data: dict) -> None:
    source_id = data.get("source_id")
    note = (data.get("note") or "").strip()
    sess = memory_sessions.get(source_id) if source_id else None
    if not sess:
        await _send_memory_error(ws, "no source in session; re-upload or re-paste")
        return
    hint = f"User correction note: {note}" if note else None
    await _run_memory_extract(ws, text=sess.text, hint=hint, source_id=source_id)


async def _handle_memory_save(ws, data: dict) -> None:
    rows_raw = data.get("rows") or []
    rows: list[ProposedFact] = []
    for r in rows_raw:
        try:
            rows.append(ProposedFact.model_validate(r))
        except Exception as e:
            await _send_memory_error(ws, f"invalid row: {e}")
            return

    # Guard: every conflict row needs a resolution.
    for row in rows:
        if row.conflicts and row.resolution is None:
            await _send_memory_error(
                ws,
                f"row {row.row_id} has a conflict but no resolution chosen",
            )
            return

    results: list[MemorySaveResult] = []
    source_id = data.get("source_id")
    for row in rows:
        try:
            if row.conflicts and row.resolution == "keep_existing":
                results.append(MemorySaveResult(row_id=row.row_id, ok=True))
                continue
            if row.conflicts and row.resolution == "replace":
                # Replace each conflicting fact with the new content.
                fact_id = None
                for c in row.conflicts:
                    fact_id = await correct_fact(c.fact_id, row.content)
                results.append(MemorySaveResult(row_id=row.row_id, ok=True, fact_id=fact_id))
                continue
            # Plain remember (new entity, no conflict, or keep_both).
            fid = await remember_row(row)
            results.append(MemorySaveResult(row_id=row.row_id, ok=True, fact_id=fid))
        except Exception as e:
            results.append(MemorySaveResult(row_id=row.row_id, ok=False, error=str(e)))

    # If fully successful and we had a source_id, drop the session.
    if source_id and all(r.ok for r in results):
        memory_sessions.pop(source_id, None)

    await ws.send_json({
        "type": "memory_saved",
        "results": [r.model_dump() for r in results],
    })
```

Finally, wire the three into the websocket dispatcher. Find the `elif msg_type == "..."` chain at ~line 95 and add:

```python
            elif msg_type == "memory_extract":
                await _handle_memory_extract(ws, data)
            elif msg_type == "memory_reextract":
                await _handle_memory_reextract(ws, data)
            elif msg_type == "memory_save":
                await _handle_memory_save(ws, data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_memory_ws_handlers.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Run full suite + lint**

Run: `.venv/bin/python3 -m pytest tests/ -q && ruff check .`
Expected: all green. Fix anything regressed.

- [ ] **Step 6: Commit**

```bash
git add web/main.py tests/test_memory_ws_handlers.py
git commit -m "feat(memory-tab): websocket handlers for extract/reextract/save with conflict routing"
```

---

## Task 7: Frontend — tab, drop zone, review table

**Files:**
- Modify: `web/static/index.html`

No unit tests here — JS has no test runner in this repo. Manual verification at the end.

- [ ] **Step 1: Add the tab button**

Find `.sidebar-tabs` at ~line 836. Currently:

```html
    <div class="sidebar-tabs">
      <button class="active" onclick="switchTab('tasks')" id="tab-tasks">Tasks</button>
      <button onclick="switchTab('freeform')" id="tab-freeform">Freeform</button>
    </div>
```

Add a third button:

```html
    <div class="sidebar-tabs">
      <button class="active" onclick="switchTab('tasks')" id="tab-tasks">Tasks</button>
      <button onclick="switchTab('freeform')" id="tab-freeform">Freeform</button>
      <button onclick="switchTab('memory')" id="tab-memory">Memory</button>
    </div>
```

- [ ] **Step 2: Add the sidebar + main panels**

After the freeform sidebar panel (`#freeform-panel`) and the freeform main panel (`#freeform-main`), add:

```html
    <!-- Memory tab sidebar (minimal — just a header, the real UI is in the main pane) -->
    <div id="memory-panel" class="hidden" style="padding: 12px; color: var(--dim); font-size: 12px;">
      Drop files, paste notes, or upload PDFs on the right. Extracted facts go to team-memory after review.
    </div>
```

And after `#freeform-main`:

```html
    <div id="memory-main" class="hidden" style="flex: 1; padding: 16px; overflow: auto;">
      <div class="memory-dropzone" id="memory-dropzone">
        <input type="file" id="memory-file" accept=".md,.txt,.log,.pdf" style="display:none;">
        <button onclick="document.getElementById('memory-file').click()">Choose file</button>
        <span class="hint">or drag a file here</span>
        <div class="hint" style="margin-top:8px;">or paste text below</div>
        <textarea id="memory-paste" rows="6" placeholder="Paste meeting notes, Slack thread, email body..."></textarea>
        <input type="text" id="memory-hint" placeholder="Context hint (optional) — e.g. 'from Thursday standup'">
        <button id="memory-extract-btn" onclick="memoryExtract()">Extract</button>
        <div id="memory-status" class="hint"></div>
      </div>

      <div id="memory-review" class="hidden" style="margin-top: 16px;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h3 id="memory-review-header">Proposed facts</h3>
          <div>
            <button onclick="memoryReextract()">Re-extract with note…</button>
          </div>
        </div>
        <table id="memory-table" class="memory-table">
          <thead><tr><th>Entity</th><th>Type</th><th>Kind</th><th>Content</th><th></th></tr></thead>
          <tbody></tbody>
        </table>
        <div style="margin-top:8px;">
          <button onclick="memoryAddRow()">+ Add row</button>
        </div>
        <div style="margin-top:12px; display:flex; gap:8px; justify-content:flex-end;">
          <button onclick="memoryDiscard()">Discard</button>
          <button id="memory-save-btn" onclick="memorySave()">Save all</button>
        </div>
      </div>
    </div>
```

- [ ] **Step 3: Add minimal styles**

Find the `<style>` block (around line 250 where `.sidebar-tabs` lives). Append:

```css
    .memory-dropzone {
      border: 1px dashed var(--border);
      border-radius: 6px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .memory-dropzone.drag-over { border-color: var(--accent); background: rgba(255,255,255,0.02); }
    .memory-dropzone textarea { width: 100%; font-family: inherit; font-size: 13px; }
    .memory-dropzone input[type=text] { width: 100%; }
    .memory-table { width: 100%; border-collapse: collapse; margin-top: 8px; }
    .memory-table th, .memory-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .memory-table .entity-badge { font-size: 10px; padding: 1px 4px; border-radius: 3px; margin-left: 4px; }
    .memory-table .entity-badge.new { background: #335; }
    .memory-table .entity-badge.exists { background: #353; }
    .memory-table tr.conflict { background: rgba(200,100,100,0.08); }
    .memory-table textarea, .memory-table input, .memory-table select {
      width: 100%; background: transparent; color: var(--text); border: 1px solid transparent; font: inherit;
    }
    .memory-table textarea:focus, .memory-table input:focus, .memory-table select:focus { border-color: var(--border); }
    .conflict-resolver { font-size: 12px; margin-top: 6px; padding: 6px; background: rgba(0,0,0,0.2); border-radius: 4px; }
    .conflict-resolver label { display: block; margin: 2px 0; }
    .row-status { font-size: 11px; }
    .row-status.ok { color: #4a4; }
    .row-status.err { color: #c44; }
```

- [ ] **Step 4: Update `switchTab()`**

Find `switchTab(tab)` at ~line 987 and extend it to handle `"memory"`:

```javascript
    function switchTab(tab) {
      currentTab = tab;
      for (const t of ["tasks", "freeform", "memory"]) {
        document.getElementById("tab-" + t).classList.toggle("active", tab === t);
      }
      document.getElementById("tasks-panel").classList.toggle("hidden", tab !== "tasks");
      document.getElementById("freeform-panel").classList.toggle("visible", tab === "freeform");
      document.getElementById("memory-panel").classList.toggle("hidden", tab !== "memory");

      document.getElementById("tasks-main").style.display = (tab === "tasks") ? "flex" : "none";
      document.getElementById("freeform-main").classList.toggle("hidden", tab !== "freeform");
      document.getElementById("memory-main").classList.toggle("hidden", tab !== "memory");

      if (tab === "freeform") {
        // KEEP the existing freeform load logic here verbatim — do not delete it.
        // Open the file, copy the inside of the current `if (tab === "freeform")`
        // block, and paste it in this spot.
      }
    }
```

The only NEW lines in `switchTab` are the ones mentioning `"memory"` / `memory-panel` / `memory-main` and the loop over `["tasks","freeform","memory"]`. Preserve everything else.

- [ ] **Step 5: Add memory JS**

In the existing `<script>` block (near other top-level JS), append:

```javascript
    // ─── Memory tab ───────────────────────────────────────────────────
    let memorySourceId = null;
    let memoryRows = [];

    function memorySetStatus(msg, isError) {
      const el = document.getElementById("memory-status");
      el.textContent = msg || "";
      el.style.color = isError ? "#c44" : "";
    }

    async function memoryUploadFile(file) {
      memorySetStatus("Uploading…");
      const fd = new FormData();
      fd.append("file", file);
      const resp = await fetch("/memory/upload", { method: "POST", body: fd });
      if (!resp.ok) {
        memorySetStatus("Upload failed: " + await resp.text(), true);
        return null;
      }
      const { source_id } = await resp.json();
      memorySourceId = source_id;
      memorySetStatus("Uploaded. Click Extract.");
      return source_id;
    }

    async function memoryExtract() {
      const pasted = document.getElementById("memory-paste").value.trim();
      const hint = document.getElementById("memory-hint").value.trim();
      const fileEl = document.getElementById("memory-file");
      let payload = { type: "memory_extract", context_hint: hint || undefined };

      if (memorySourceId) {
        payload.source_id = memorySourceId;
      } else if (fileEl.files[0]) {
        const sid = await memoryUploadFile(fileEl.files[0]);
        if (!sid) return;
        payload.source_id = sid;
      } else if (pasted) {
        payload.pasted_text = pasted;
      } else {
        memorySetStatus("Choose a file or paste some text first.", true);
        return;
      }
      memorySetStatus("Extracting…");
      ws.send(JSON.stringify(payload));
    }

    async function memoryReextract() {
      if (!memorySourceId) {
        memorySetStatus("Re-extract needs an uploaded file. Paste is re-extracted via Extract.", true);
        return;
      }
      const note = prompt("Correction note for the agent:");
      if (!note) return;
      memorySetStatus("Re-extracting…");
      ws.send(JSON.stringify({
        type: "memory_reextract", source_id: memorySourceId, note,
      }));
    }

    function memoryRenderRows() {
      const tbody = document.querySelector("#memory-table tbody");
      tbody.innerHTML = "";
      memoryRows.forEach((row, i) => {
        const tr = document.createElement("tr");
        if (row.conflicts && row.conflicts.length) tr.classList.add("conflict");
        tr.innerHTML = `
          <td>
            <input value="${escAttr(row.entity)}" onchange="memoryRows[${i}].entity=this.value">
            <span class="entity-badge ${row.entity_status || 'new'}">${row.entity_status || 'new'}</span>
          </td>
          <td>
            <select onchange="memoryRows[${i}].entity_type=this.value">
              ${["project","concept","person","repo","system"].map(t =>
                `<option ${row.entity_type===t?'selected':''}>${t}</option>`).join("")}
            </select>
          </td>
          <td>
            <select onchange="memoryRows[${i}].kind=this.value">
              ${["decision","architecture","gotcha","status","preference","fact"].map(k =>
                `<option ${row.kind===k?'selected':''}>${k}</option>`).join("")}
            </select>
          </td>
          <td>
            <textarea rows="2" onchange="memoryRows[${i}].content=this.value">${escHTML(row.content)}</textarea>
            ${(row.conflicts||[]).length ? memoryRenderConflict(row, i) : ""}
            <span class="row-status" id="row-status-${i}"></span>
          </td>
          <td><button onclick="memoryDeleteRow(${i})">✕</button></td>
        `;
        tbody.appendChild(tr);
      });
      document.getElementById("memory-review").classList.remove("hidden");
      document.getElementById("memory-review-header").textContent = `Proposed facts (${memoryRows.length})`;
      memoryUpdateSaveButton();
    }

    function memoryRenderConflict(row, i) {
      const choices = ["keep_existing","replace","keep_both"];
      const labels = {
        keep_existing: "Keep existing (skip this row)",
        replace: "Replace existing with new (correct)",
        keep_both: "Keep both",
      };
      const existing = row.conflicts.map(c => `• <em>existing:</em> ${escHTML(c.existing_content)}`).join("<br>");
      return `
        <div class="conflict-resolver">
          <div style="margin-bottom:4px;">⚠ Conflict with existing fact(s):<br>${existing}</div>
          ${choices.map(c => `
            <label>
              <input type="radio" name="res-${i}" value="${c}"
                ${row.resolution===c?'checked':''}
                onchange="memoryRows[${i}].resolution=this.value; memoryUpdateSaveButton();">
              ${labels[c]}
            </label>`).join("")}
        </div>`;
    }

    function memoryUpdateSaveButton() {
      const unresolved = memoryRows.filter(r => r.conflicts && r.conflicts.length && !r.resolution).length;
      const btn = document.getElementById("memory-save-btn");
      btn.disabled = unresolved > 0;
      btn.textContent = unresolved > 0
        ? `Save all (${unresolved} conflict${unresolved>1?'s':''} need review)`
        : "Save all";
    }

    function memoryAddRow() {
      memoryRows.push({
        row_id: "r-" + Math.random().toString(36).slice(2, 10),
        entity: "", entity_type: "concept", entity_status: "new",
        kind: "fact", content: "", conflicts: [], resolution: null,
      });
      memoryRenderRows();
    }

    function memoryDeleteRow(i) {
      memoryRows.splice(i, 1);
      memoryRenderRows();
    }

    function memorySave() {
      ws.send(JSON.stringify({
        type: "memory_save",
        rows: memoryRows,
        source_id: memorySourceId,
      }));
      memorySetStatus("Saving…");
    }

    function memoryDiscard() {
      memoryRows = [];
      memorySourceId = null;
      document.getElementById("memory-paste").value = "";
      document.getElementById("memory-hint").value = "";
      document.getElementById("memory-file").value = "";
      document.getElementById("memory-review").classList.add("hidden");
      memorySetStatus("");
    }

    // drag-and-drop
    (function wireMemoryDrop() {
      const dz = document.getElementById("memory-dropzone");
      if (!dz) return;
      dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("drag-over"); });
      dz.addEventListener("dragleave", () => dz.classList.remove("drag-over"));
      dz.addEventListener("drop", async e => {
        e.preventDefault(); dz.classList.remove("drag-over");
        const f = e.dataTransfer.files[0]; if (!f) return;
        document.getElementById("memory-file").files = e.dataTransfer.files;
        await memoryUploadFile(f);
      });
    })();

    // helpers — these may already exist in the file; reuse if so.
    function escHTML(s) {
      return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    }
    function escAttr(s) { return escHTML(s).replace(/"/g,"&quot;"); }
```

Important: `escHTML` and `escAttr` likely already exist in this file (search for them). If they do, skip the duplicate definitions.

- [ ] **Step 6: Handle memory messages on the websocket**

Find the websocket `onmessage` handler (search for `ws.onmessage` or a similar dispatcher). Add cases:

```javascript
      else if (msg.type === "memory_rows") {
        memoryRows = msg.rows;
        if (msg.source_id) memorySourceId = msg.source_id;
        memorySetStatus("");
        memoryRenderRows();
      } else if (msg.type === "memory_saved") {
        let allOk = true;
        msg.results.forEach((r, i) => {
          const el = document.getElementById("row-status-" + i);
          if (el) {
            el.textContent = r.ok ? "✓ saved" : "✕ " + (r.error || "failed");
            el.className = "row-status " + (r.ok ? "ok" : "err");
          }
          if (!r.ok) allOk = false;
        });
        if (allOk) { memorySetStatus("All saved."); memoryDiscard(); }
        else memorySetStatus("Some rows failed — see statuses.", true);
      } else if (msg.type === "memory_error") {
        memorySetStatus(msg.message, true);
      }
```

- [ ] **Step 7: Manual verification**

Per `CLAUDE.md`: "For UI or frontend changes, start the dev server and use the feature in a browser". Run:

```bash
docker compose up -d
docker compose exec auto-agent alembic upgrade head
```

Open the app in a browser. Click the **Memory** tab. Test in order:

1. Paste some text (e.g. a few sentences about a decision), click Extract → review table appears with proposed rows.
2. Edit a row's entity and content inline → changes persist in `memoryRows`.
3. Click + Add row → blank row appears, editable.
4. Click ✕ on a row → row disappears.
5. Click Save all → rows turn ✓; confirm in team-memory (e.g. via `recall` through the MCP) that facts landed.
6. Drop a small `.md` file → uploads, Extract works.
7. Drop a small PDF → uploads, parses, Extract works.
8. Drop a `.xyz` file → 400 error visible in the status line.
9. Paste > 200k chars → error visible.
10. Trigger a conflict manually: pick an entity name that already has a fact of the same kind; write content that contradicts it; confirm the row becomes a conflict row with the three radio choices and that "Save all" is disabled until you pick one.
11. Pick "Replace with new" → save → confirm in team-memory the existing fact was superseded via `correct`.

Write down which of these passed / failed and report honestly.

- [ ] **Step 8: Lint**

Run: `ruff check .` (no Python changes in this task, so should be clean).

- [ ] **Step 9: Commit**

```bash
git add web/static/index.html
git commit -m "feat(memory-tab): frontend tab, drop zone, review table, conflict resolver"
```

---

## Task 8: Full regression + final commit

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all pass. If anything unrelated broke, stop and investigate.

- [ ] **Step 2: Lint + format**

Run:
```bash
ruff check .
ruff format --check .
```
Fix anything flagged.

- [ ] **Step 3: Self-review the diff**

Run: `git diff main...HEAD`
Check for:
- Accidental debug prints or `console.log`
- Unused imports
- Missing docstrings on public functions
- Any backwards-compat shims for things that don't need them (per `CLAUDE.md`, remove these)
- Files > 500 lines — if any module exceeded that, consider a split.

- [ ] **Step 4: Final sanity manual test**

Repeat the happy-path from Task 7 Step 7 once more end-to-end.

- [ ] **Step 5: Open PR**

Follow the project's PR convention. Include the spec link in the description and the "what I manually tested" checklist from Task 7.

---

## Rollback / safety notes

- The memory tab writes to the shared team-memory store. A buggy extractor + careless reviewer could spam the graph. Mitigations baked in: review-before-save, conflict gate, `source="memory-tab"` tag so bad entries can be bulk-reverted via the MCP later.
- No migrations added. If something goes wrong, revert the feature branch — no cleanup needed.
- `memory_sessions` lives only in process memory. If the web process restarts mid-session, the user loses their staged rows. Acceptable for v1.

## Spec coverage check

- Drop-zone + paste textarea + context hint: Task 7.
- Text + PDF support: Tasks 1, 5.
- Agent-assisted extraction via existing LLM provider: Task 4.
- Review table with inline edit, add, delete: Task 7.
- Re-extract with correction note: Tasks 6, 7.
- Entity match badges via `recall`: Task 6 (`_run_memory_extract` annotates each row).
- Conflict detection + per-row resolution (keep/replace/keep both) with save gate: Tasks 4, 6, 7.
- Files never persisted on disk: Task 5 (`del raw` before response; only extracted text held in RAM).
- 200k-char cap on both upload and paste: Tasks 5, 6.
- Testing (extractor, handlers, upload): Tasks 4, 5, 6.
- Non-goals (no dedupe of identical facts, no editing saved facts, no images/audio): honored — nothing implements them.
