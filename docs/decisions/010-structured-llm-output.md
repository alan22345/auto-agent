# 010 — Single owner of "LLM text reply → dict"

## Status
Accepted

## Context
Five sites in the codebase asked the LLM for a JSON object and parsed the
reply: `agent/po_analyzer.py`, `agent/architect_analyzer.py`,
`agent/classifier.py`, `agent/memory_extractor.py`, and
`agent/lifecycle/intent.py`. Each one re-implemented the same flow
slightly differently:

- `po_analyzer._parse_analysis_output` and
  `architect_analyzer._parse_analysis_output` were byte-for-byte
  identical: strip `` ``` ``-prefixed lines, locate first `{` / last `}`,
  `json.loads`, log + return `None` on failure.
- `intent.extract_intent` stripped fences inline, by splitting on the
  first newline and trimming a trailing `` ``` ``.
- `memory_extractor._parse_response` stripped fences a third way:
  `split("```", 2)[1]`, then look for a `json` language tag, then
  `rsplit("```", 1)[0]`. It also owned a one-off retry loop with a
  "your previous response was not valid JSON" nudge.
- `classifier._classify_with_llm` skipped fence-stripping entirely. When
  the model returned `` ```json{...}``` ``, the brace-locate succeeded
  but `json.loads` raised on the trailing `` ``` ``, the outer `try` ate
  the exception, and the classifier silently fell back to keyword
  heuristics. A latent bug, hidden by the broader try/except.

The cost of duplication wasn't aesthetic — it was that one of the five
copies was wrong, and there was nowhere central to fix it.

## Decision
Introduce `agent/llm/structured.py` as the single owner of
"LLM text reply → dict". Two top-level functions:

- `parse_json_response(text) -> dict | None` — pure. Strips a leading
  triple-backtick block (with or without a `json` language tag), locates
  the first `{` / last `}`, `json.loads` the slice, returns `None` on any
  failure (including a top-level JSON list — all five callers want
  objects). Never raises. Callers pick the fallback policy:
  - po_analyzer / architect_analyzer: `None` → log + early-return,
  - intent.extract_intent: `None` → `{}` (non-blocking),
  - classifier: `None` → ValueError → outer try → heuristic fallback.
- `complete_json(provider, messages, *, system, retries=2, ...)` —
  one-shot LLM call + parse + bounded retry. On attempt 2+, appends
  "Your previous response was not valid JSON" to `system`. Raises
  `ValueError` after exhausting retries. Generalises the loop that lived
  in `memory_extractor.extract`. No `schema_hint`/`format_hint`
  parameter: zero callers want it today, so adding it would fail the
  deletion test ("one adapter is hypothetical, two is real").

All five call sites delete their helpers and route through these.

**Deepening choice**: deepened the LLM seam. The seam previously hosted
only `anthropic_mapper.py` (chat-message wire-format translation, ADR-006).
It now hosts both "chat completion" (the mapper) and "structured one-shot"
(this module) — two real adapters under the same conceptual seam. The new
module is small (~60 lines) but each line is dense — fence stripping, brace
location, retry loop, nudge text — and removing it would push that exact
shape into five callers again. The deletion test passes.

## Consequences

**Trade-offs**
- One extra module under `agent/llm/`. Five callers shrink; net code
  count drops. The LLM seam grows by one file, intentionally.
- `complete_json` is a free function taking a provider, not a method on
  `LLMProvider`. Reason: putting it on the provider would force the
  `claude_cli` passthrough provider (which doesn't ship JSON; it drives
  Claude Code as a black box) to implement structured output it doesn't
  use. Mirrors the precedent in ADR-006: the provider interface stays
  narrow; specialised behaviour lives in free functions over a provider.
- `parse_json_response` rejects top-level JSON lists. Previously each
  caller would silently get `[...]` and crash on `data.get("suggestions")`.
  The shared parser surfaces this as `None` instead — the caller's chosen
  fallback path fires, with a logged warning rather than an AttributeError.
- The classifier's previously-silent fence-stripping bug now actually
  works. `tests/test_agent_classifier.py::test_classifier_handles_fenced_json`
  pins the regression.

**Rejected alternatives**
- *Method on `LLMProvider`*: forces every provider (incl. passthrough) to
  implement structured output; widens the seam without justification.
  See ADR-006: prefer narrow provider interfaces, push specialised
  behaviour into free functions over a provider.
- *Pydantic-validated output (typed schemas instead of `dict`)*: all five
  callers want loose dicts and do their own field plucking; jumping to
  typed schemas is a much bigger refactor and not what the duplication
  problem demands. A future ADR can revisit per-caller, e.g. typing the
  PO analyzer's suggestion shape.
- *External library (`instructor`, `outlines`)*: extra dependency for
  ~60 lines of code. Locality > leverage. The fence-stripping rules are
  also load-bearing in a way an external library wouldn't track — we own
  the exact behaviour and its tests.
- *Single mega-helper that bundles both pure parse and one-shot LLM call*:
  the two callers that route through the agent loop (`po_analyzer`,
  `architect_analyzer`) only want the parse step; bundling would force
  them through a provider abstraction they don't need. Two functions
  matches the two real shapes.
