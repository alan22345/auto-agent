# Bigger PO + Market Research — Design Spec

**Date:** 2026-05-12
**Status:** Approved (pending user review of this written spec)
**Scope:** Sub-project A of a 3-spec overhaul. Sub-projects B (architect/builder/reviewer trio) and C (freeform self-verification) are out of scope for this spec.

## Problem

The Product Owner agent (`agent/po_analyzer.py`) consistently produces tactical, button-sized suggestions ("add a search icon", "show a tooltip") rather than feature-shaped or modality-shaped opportunities (e.g. "add voice input to the chat flow"). Three root causes, treated as one:

1. **No competitive context** — the PO is bounded by what exists in the repo, so suggestions are by definition incremental.
2. **No modality awareness** — the prompt implicitly says "look at existing flows", which never surfaces voice/vision/AI-native angles.
3. **No strategic frame** — without external "why now" signal, the PO can't tell a generic polish from a market opportunity.

These three are different symptoms of one root cause: **the PO has no grounding outside the codebase**. The fix is to give it that grounding via a dedicated market-research agent that runs **before** the PO and produces a versioned, sourced brief that the PO must cite to ground its suggestions.

## Out of scope (deferred)

- Cold-start "build a new repo demo-grade" architect/builder/reviewer trio (sub-project B).
- Freeform self-verification: run + visual-feedback loop (sub-project C).
- Market research for `architect_analyzer.py` suggestions. Architecture suggestions are internal/technical; market context wouldn't change the answer. Researcher is PO-only.
- Brief history UI / past-briefs page.
- Per-finding "promote to suggestion" UX.
- Per-Suggestion section-level evidence linking (which *part* of the brief justified a suggestion). `Suggestion.brief_id` gives brief-level provenance; section-level can be added later without migration.

## Architecture

A1: two separate agent invocations, chained. The researcher runs as an inline step inside the existing PO cron loop in `agent/po_analyzer.py`; it is **not** its own cron.

```
po_analyzer.run_po_analysis_loop  (cron)
  └─ _check_and_analyze
       ├─ for each due FreeformConfig:
       │    ├─ brief = latest MarketBrief for repo
       │    ├─ if brief is None or older than market_brief_max_age_days:
       │    │     brief = await run_market_research(session, config, repo)   # NEW
       │    │     if brief is None and no prior brief:
       │    │         publish(po_analysis_failed(reason="no brief"))
       │    │         advance last_analysis_at  # back-off
       │    │         continue
       │    └─ await handle_po_analysis(session, config, brief=brief)
```

Researcher and PO are two distinct AgentLoop runs with separate prompts, separate token budgets, separate failure modes, and separate cost telemetry.

## Data model

### New table: `market_briefs`

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `repo_id` | int FK → `repos.id`, indexed | |
| `organization_id` | int FK → `organizations.id`, indexed | Parity with `Suggestion`/`FreeformConfig`. |
| `created_at` | timestamptz | |
| `product_category` | text | Agent's inferred product category, e.g. "AI dev tools". |
| `competitors` | jsonb | `[{name, url, why_relevant}, ...]` |
| `findings` | jsonb | `[{theme, observation, sources: [url, ...]}, ...]` |
| `modality_gaps` | jsonb | `[{modality, opportunity, sources: [...]}, ...]` — voice/vision/AI-native lens. |
| `strategic_themes` | jsonb | `[{theme, why_now, sources: [...]}, ...]` — the "why now" lens. |
| `summary` | text | Short prose digest the PO reads. |
| `raw_sources` | jsonb | `[{url, title, fetched_at}, ...]` — every URL the agent passed to `fetch_url`, collected from `WorkspaceState`, not from the agent's own output. |
| `partial` | bool default false | True when the researcher hit the turn cap before completing. |
| `agent_turns` | int | Cost telemetry. |

`Repo` gains `market_briefs = relationship(...)`. No FK from `Suggestion` to brief at the table level beyond `brief_id` below.

### `FreeformConfig` — additions

| column | type | notes |
|---|---|---|
| `last_market_research_at` | timestamptz nullable | Independent timestamp from `last_analysis_at`. |
| `market_brief_max_age_days` | int default 7 | Briefs younger than this are re-used; older triggers re-research. |

### `Suggestion` — additions

| column | type | notes |
|---|---|---|
| `evidence_urls` | jsonb default `'[]'` | `[{url, title, excerpt}, ...]` — what the PO cited. |
| `brief_id` | int FK → `market_briefs.id`, nullable | Which brief grounded this suggestion. Nullable because bug-category suggestions don't need a brief. |

All additions are backwards-compatible: nullable columns, defaulted jsonb. No data migration needed.

## Components

### `agent/market_researcher.py` (new)

Mirrors `agent/po_analyzer.handle_po_analysis` shape:

- One async function `run_market_research(session, config, repo) -> MarketBrief | None`.
- Spins up `create_agent(workspace, readonly=True, max_turns=20, ...)` with the existing readonly tool set **plus** `web_search` and `fetch_url`.
- Builds the researcher prompt (see "Researcher prompt" below) and runs the agent loop.
- Parses output with `parse_json_response`.
- Collects `raw_sources` from `WorkspaceState` (every URL the agent fetched, not what it wrote in its own output) — this avoids the known failure mode where agents drop source URLs under context pressure.
- Writes the `MarketBrief` row.
- Returns the row, or `None` on failure (web tools down, no parseable output).
- Publishes events: `market_research_started`, `market_research_completed`, `market_research_failed`.

### `agent/po_analyzer.py` (modified)

- `_check_and_analyze` orchestrates the chain (see Architecture above).
- `handle_po_analysis` gains a **required** `brief: MarketBrief` parameter. The PO is never called without a brief — if research fails and no prior brief exists, the cycle is skipped in `_check_and_analyze`. The Optional shape would be a lie: the new prompt rules + post-parse filter make a no-brief PO degenerate (it can only emit bug-category suggestions). No internal caller depends on a default; `architecture_analyzer` runs its own pipeline.
- `build_po_analysis_prompt` (in `agent/prompts.py`) gains a required `brief` parameter and renders it as the "Market context" section.
- After parsing PO output, a **post-parse filter** drops suggestions that have `evidence_urls=[]` and `category != "bug"`. This filter is one of the two load-bearing mechanisms enforcing "no ungrounded button-sized suggestions" — the prompt asks for grounding, the filter enforces it.
- Each persisted `Suggestion` gets `brief_id = brief.id` and `evidence_urls = s["evidence_urls"]` (defaults to `[]`).

### `agent/prompts.py` (modified)

`PO_ANALYSIS_PROMPT` gets a new `market_context` section in its body. A brief is always provided (see `handle_po_analysis` above), so this section is unconditional. The instructions gain three rules:

> For EACH suggestion you propose:
> - It must be motivated by at least one item from the Market context OR be an obvious bug/UX defect (in which case category="bug" and evidence_urls=[]).
> - Prefer suggestions that introduce a new capability or modality the repo currently lacks over suggestions that polish what already exists.
> - Cite the URLs your suggestion draws from in `evidence_urls`. Drop suggestions you can't ground in either market evidence or a visible repo defect.

The JSON output schema for each suggestion gains a required `evidence_urls` field.

A new prompt constant `MARKET_RESEARCH_PROMPT` lives alongside `PO_ANALYSIS_PROMPT`. It is structured as four phases inside the single prompt:

1. **Anchor** — read `README.md` (first ~100 lines), `CONTEXT.md` (if present), and `glob` of top-level routes/pages **by filename only** (not contents). Explicitly do **not** read `package.json` or any large manifest. Output an inferred product description and category.
2. **Discover competitors** — web_search the category + adjacent terms, pick 3–5 representative products, fetch_url each one's landing/features page.
3. **Three lenses** — for each lens (competitive / modality / strategic), search and synthesize, every claim tagged with the URL it came from.
4. **Synthesize the brief** — output the JSON. Hard rule: no claim without a source URL — "if you can't cite it, drop it."

### `shared/models.py` (modified)

New `MarketBrief` ORM model. Additions to `FreeformConfig` and `Suggestion` per the data model above.

### `shared/events.py` (modified)

New event builders: `market_research_started`, `market_research_completed`, `market_research_failed`. Registered in `agent/lifecycle/_orchestrator_api.py` alongside the existing analysis events.

### `orchestrator/router.py` (modified)

One new endpoint: `GET /api/repos/:id/market-brief/latest` → returns the latest `MarketBrief` row as JSON, or 404 if none exists. No list endpoint yet — brief history is deferred.

### `web-next/` (modified)

Two changes:

1. **Suggestion card** — when `Suggestion.evidence_urls` is non-empty, render a "Backed by" footer with up-to-3 source links (title + favicon if cheap). Existing component, extended.
2. **Suggestions list page header** — "Latest brief from {date}" link that opens a modal showing the brief summary, competitors, findings, modality gaps, and strategic themes. Pulls from the new endpoint.

Brief history page, freeform-config tuning of `market_brief_max_age_days`, and per-section evidence drill-down are all deferred.

## Data flow

1. Cron fires in `run_po_analysis_loop`.
2. `_check_and_analyze` finds a due `FreeformConfig`.
3. Brief freshness check: if no brief or brief older than `market_brief_max_age_days`:
   a. `run_market_research` clones the repo (readonly workspace), runs the researcher agent.
   b. Researcher emits `market_research_started`, runs the 4-phase prompt with web+readonly tools.
   c. Output parsed; `raw_sources` collected from `WorkspaceState`; `MarketBrief` row written.
   d. Emits `market_research_completed`.
4. `handle_po_analysis` runs with the brief.
5. PO clones repo (separate workspace), runs the agent with the brief rendered into the prompt.
6. Output parsed; post-parse filter drops ungrounded suggestions.
7. Each surviving suggestion is persisted with `brief_id` + `evidence_urls`.
8. Existing flow (auto-approve, team-memory promotion, `po_suggestions_ready` event) continues unchanged.

## Error handling

| Failure | Behavior |
|---|---|
| `BRAVE_API_KEY` missing / Brave API errors | Researcher logs, emits `market_research_failed`. Chain falls back to the previous brief if one exists, else skips this PO cycle and advances `last_analysis_at` (back-off). |
| `fetch_url` fails for a competitor page | Agent continues with the remaining competitors. Brief is still produced; possibly with `partial: true`. |
| Researcher hits turn cap | Whatever JSON it produced is parsed if possible. If parseable, brief is written with `partial: true`. If not parseable, treated as research failure. |
| Researcher output not parseable | Treated as research failure (see first row). |
| PO output not parseable | Existing behavior unchanged — `po_analysis_failed` event. |
| PO produces ungrounded suggestions | Post-parse filter drops them. Filter is logged so we can see how often the prompt is violated. |

The back-off uses the existing `_FAILURE_BACKOFF_NOW = True` pattern — `last_analysis_at` advances on failure to prevent retry hammering against a broken repo.

## Testing

Following the patterns in `tests/test_architecture_mode.py` and `tests/test_grill_before_planning.py`.

### Unit tests

- **`tests/test_market_researcher.py`**
  - `test_researcher_writes_brief_with_sources`: stub LLM + web tools, assert `MarketBrief` row exists and `raw_sources` matches the URLs actually fetched.
  - `test_researcher_failure_returns_none`: stub missing `BRAVE_API_KEY`, assert `None` + `market_research_failed` published.
  - `test_researcher_partial_when_turn_cap_hit`: stub turn cap, assert brief written with `partial: true`.
  - `test_researcher_skips_package_json`: assert the rendered researcher prompt does **not** contain the string "package.json" in its anchor instructions (regression for the token-cost decision).
- **`tests/test_market_brief_freshness.py`** — pure logic test of `_brief_is_fresh(brief, now, max_age_days)`, extracted as a free function for isolated testability.

### Integration tests

- **`tests/test_po_with_market_research.py`**
  - Stale brief: researcher runs, PO runs, suggestions carry `brief_id` + `evidence_urls`.
  - Fresh brief in DB: researcher **not** called (mock counter assertion); PO runs with existing brief.
  - Research fails + prior brief exists: PO runs with prior brief, no analysis cycle wasted.
  - Research fails + no prior brief: PO skipped, `po_analysis_failed(reason="no brief")` published, `last_analysis_at` advanced.

### Regression test (load-bearing)

- **`tests/test_po_drops_ungrounded_suggestions.py`**
  - Feed PO a fixture brief + fixture LLM response with: 2 grounded suggestions, 1 bug with `evidence_urls=[]` (allowed), 1 "add a button" with `evidence_urls=[]` and no bug.
  - Assert only the first 3 persist; the fourth is filtered.
  - This test is the gating reason we cannot regress to button-sized suggestions even if the LLM ignores the prompt.

### Not tested here

- Brave API real behavior — covered by existing `tests/test_web_search_tool.py`.
- PO suggestion *quality* — eval territory (`eval/`), not unit-test territory. A follow-up task adds a market-research-aware case to the agent eval.

## Migrations

One Alembic migration:

- Add `market_briefs` table.
- Add `last_market_research_at` and `market_brief_max_age_days` columns to `freeform_configs`.
- Add `evidence_urls` and `brief_id` columns to `suggestions`.
- Add FKs.

All columns nullable or defaulted; no data backfill needed.

## Acceptance criteria

1. Every PO cron run produces a `MarketBrief` (or reuses a fresh one) before any suggestion is generated. Every non-`bug` suggestion has non-empty `evidence_urls` and a non-null `brief_id`. The PO is never invoked without a brief.
2. When a fresh `MarketBrief` (within `market_brief_max_age_days`) exists, the researcher does not run on the next PO cycle for that repo.
3. When the researcher fails and no prior brief exists, the PO cycle is skipped with `po_analysis_failed(reason="no brief")` and `last_analysis_at` advances.
4. `web-next/` Suggestion card displays evidence links for grounded suggestions; "View market brief" link on the suggestions list page opens a modal with brief contents.
5. The regression test `test_po_drops_ungrounded_suggestions` passes — ungrounded non-bug suggestions are filtered.
6. The full existing test suite (`tests/`) still passes.

## Open questions / follow-ups (not blocking implementation)

- Should `market_brief_max_age_days` be tunable in the freeform-config UI before launch, or after first usage data?
- Eval task case for "PO + brief" — added in a follow-up after this spec ships.
- Future: brief history page, per-finding "promote to task" UX. Already deferred above.
