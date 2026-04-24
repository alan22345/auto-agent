# Memory Tab — Design

**Date:** 2026-04-24
**Status:** Approved for implementation plan

## Goal

Give teammates a UI in auto-agent to drop files, PDFs, or pasted text and have an LLM extract structured facts that they can review, correct, and then save into the shared `team-memory` graph. Today the only way to write to team-memory is for an agent to call the MCP mid-task — there is no teammate-facing ingestion path.

## Scope

**In:**
- New "Memory" tab in the existing single-page web UI, alongside Tasks and Freeform.
- File upload (`.txt`, `.md`, `.log`, `.pdf`) plus a paste-a-big-chunk textarea.
- Agent-assisted extraction: LLM proposes `(entity, kind, content)` rows.
- Review table with inline edit, add row, delete row, re-extract with a correction note.
- Per-row Save results that call `team-memory` `remember`.

**Out (v1):**
- Dedupe against existing facts on save.
- Editing or deleting already-saved facts from this UI (use MCP `correct` instead).
- Images, audio, `.docx`, `.csv`.
- Bulk / folder imports.
- Persistence of in-flight extractions across page reloads.

## Architecture

```
web/static/index.html
  └─ new "Memory" tab + drop zone + review table (vanilla JS, same style as Tasks/Freeform)

web/main.py
  ├─ POST /memory/upload            (multipart, parses to text, deletes bytes, returns source_id)
  └─ ws handlers:
     ├─ _handle_memory_extract      (runs MemoryExtractor, returns proposed rows)
     ├─ _handle_memory_reextract    (same source text + correction note)
     └─ _handle_memory_save         (writes rows via team_memory client)

agent/memory_extractor.py           (new) single structured-output LLM call → rows

shared/team_memory.py               (new) thin client for recall + remember against the
                                    team-memory Postgres store (or MCP if already wired)
```

### Dependency layers

Follows the strict layering in CLAUDE.md:

- `shared/team_memory.py` sits at the `shared/` layer — DB/HTTP client only, no agent imports.
- `agent/memory_extractor.py` uses `agent/llm/` + `shared/team_memory.py` for `recall` lookups.
- `web/main.py` imports both (web is the top layer).

### Data flow (happy path)

1. Client sends `POST /memory/upload` (or skips upload for pasted text).
2. Server parses: `.pdf` via `pypdf`, text files via UTF-8. File bytes are freed the moment parsing returns. Server holds only the extracted text on the websocket session, keyed by `source_id`.
3. Client sends ws message `memory_extract {source_id | pasted_text, context_hint}`.
4. Server calls `MemoryExtractor.extract(text, hint)` → structured LLM call returning `[{entity, kind, content}]`.
5. For each distinct entity name, server calls `team_memory.recall(name)` to tag rows `exists` (with match confidence) vs `new`.
6. Server returns `{rows: [...]}`.
7. User edits rows in the UI (entity name, kind, content), deletes, or adds. Can click "Re-extract with note…" to re-run step 4 with a correction hint; replaces the table.
8. User clicks Save → ws `memory_save {rows}` → server loops `team_memory.remember(...)` once per row → returns `{results: [{ok, error?}, ...]}`.
9. UI marks each row ✓/✕; clears table on all-green; leaves failed rows in place with error tooltip.
10. On websocket disconnect, all session state is dropped.

### Resource lifecycle

Files never touch persistent disk. Flow:

- Upload → parse to text in-process → discard bytes before responding.
- Extracted text lives only on the websocket session dict, keyed by `source_id`.
- On `memory_save` success or ws disconnect, entry is deleted.
- Hard cap: 200k characters per session (configurable via env). Oversize → 400 with "split this into smaller chunks".

This deliberately avoids the repo-clone memory-bloat pattern the VM hit previously.

## Review UX

Tab sits in the existing `.sidebar-tabs` container. The main pane is a single column:

```
┌─ Drop a file, or paste text ─────────────────────────────┐
│  [drag-drop zone]    [choose file]                       │
│  ┌─ or paste here ──────────────────────────────────┐    │
│  │                                                  │    │
│  └──────────────────────────────────────────────────┘    │
│  Context hint (optional): [__________________________]   │
│  [ Extract ]                                             │
└───────────────────────────────────────────────────────────┘

Proposed facts (N)                         [ Re-extract with note… ]
┌─────────────────┬──────────────┬───────────────────────────┬───┐
│ Entity          │ Kind         │ Content                   │ ✕ │
├─────────────────┼──────────────┼───────────────────────────┼───┤
│ auto-agent ✓    │ decision  ▾  │ PO agent now runs nightly │ ✕ │
│ pg-migrations + │ gotcha    ▾  │ migration 018 must run…   │ ✕ │
└─────────────────┴──────────────┴───────────────────────────┴───┘
[ + Add row ]                        [ Discard ]  [ Save all ]
```

- Entity cell badge: `✓` = matched an existing entity via `recall` (tooltip shows matched name + score); `＋` = new entity.
- Clicking the entity cell opens a small dropdown of recall suggestions + "use new name".
- Kind dropdown fixed vocabulary: `decision | architecture | gotcha | status | preference | fact`. Defaults to `fact` when the LLM omits it.
- Content is an inline-editable textarea that grows with content.
- `+ Add row` appends a blank row (for facts the agent missed).
- `Re-extract with note…` opens a one-line input; sends `memory_reextract {source_id, note}`; replaces the table on return.

## Error handling

| Failure | Behavior |
|---|---|
| PDF parse error | Upload returns 400 with message; drop zone shows inline error. |
| LLM returns non-JSON | Retry once with a "return valid JSON" system hint. If still bad, surface raw text in a single error row the user can manually convert. |
| Input > 200k chars | Upload/extract rejected with "split into smaller chunks". |
| `team_memory.remember` fails for row N | Other rows still commit; row N stays in table with ✕ and error tooltip. |
| Websocket disconnect mid-session | Session state dropped; user re-uploads. No recovery. |
| Transient LLM / DB errors | Inherit the existing retry behavior in `agent/llm/` and SQLAlchemy; no custom retry here. |

## Testing

Per CLAUDE.md TDD: write failing tests first.

- `tests/test_memory_extractor.py`
  - Feeds a short markdown sample with a mocked LLM returning known JSON → asserts rows parsed, kind falls back to `fact` when missing, entity names stripped/normalized.
  - Malformed JSON on first call → retry once → success path.
  - Malformed twice → raises, carries raw text.

- `tests/test_memory_ws_handlers.py`
  - Fakes the team_memory client.
  - `memory_extract`: returned rows have `exists`/`new` badges based on fake `recall` results.
  - `memory_save`: one `remember` call per row; partial failure surfaces per-row; success clears session state.
  - `memory_reextract`: reuses stored text, passes note into extractor.

- `tests/test_memory_upload.py`
  - POST a small UTF-8 file → parsed, bytes freed (assert no tempfile left).
  - POST a tiny fixture PDF → parsed via pypdf.
  - Oversize payload → 400.

No JS test runner in this repo, so the review table is verified manually in the running app. Per CLAUDE.md, UI correctness claims must be backed by manual testing — this is called out explicitly in the implementation plan.

## Non-goals / explicit YAGNI

- No auth / per-user scoping — memory is already team-wide by design.
- No preview of what's already in memory for an entity (use the agent's normal `recall` flow).
- No scheduled / background ingestion. Everything is interactive.

## Open questions for implementation plan

- Does a usable `team_memory` Python client already exist in this codebase, or do we need a new `shared/team_memory.py`? The plan step 1 is to find out and either import or write.
- Which LLM provider to use for extraction — follow the project default (per `agent/llm/__init__.py::get_provider`) rather than hardcoding.
