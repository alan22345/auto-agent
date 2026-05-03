# 006 — Extract Anthropic message mapper as the LLM provider seam

## Status
Accepted

## Context
`BedrockProvider` and `AnthropicProvider` both speak the Anthropic Messages
API wire format. Each adapter held its own `_build_api_messages`,
`_to_api_tool`, and `_from_api_response` — ~150 lines of nearly identical
translation duplicated across `agent/llm/bedrock.py` and
`agent/llm/anthropic.py`.

Two problems followed from the duplication:

1. **The load-bearing batching invariant lived in two places.** Anthropic's
   API requires every `tool_result` for a single assistant turn's `tool_use`
   blocks to land in one user message's content array. Splitting them
   silently breaks the conversation — the assistant thinks its calls
   weren't answered and re-issues them ("Groundhog Day" read loop). The
   invariant is called out in CLAUDE.md, but two copies meant two ways for
   it to drift.

2. **The `LLMProvider` interface was shallow.** Each adapter knew the
   entire wire format, leaving very little hidden behind `complete()`.
   Bedrock differed from native Anthropic in only two ways: it requires
   `"type": "object"` on tool input schemas, and it retries 429/503/529.
   Everything else was copy-paste.

## Decision
Introduce `agent/llm/anthropic_mapper.py` as the single owner of
`Message ↔ Anthropic-API` translation. Three top-level functions:

- `to_api_messages(messages)` — applies the batching invariant
- `to_api_tool(tool)` — unconditionally injects `"type": "object"` when
  missing (valid for both Bedrock and native Anthropic per JSON Schema; the
  variation isn't worth a parameter)
- `from_api_response(response)` — maps SDK responses to `LLMResponse`

Both providers shrink to thin transport+auth adapters:

- `BedrockProvider` keeps the AWS client construction (bearer token →
  access keys → credential chain) and the 429/503/529 exponential-backoff
  retry loop. That retry loop is one of the four critical invariants in
  CLAUDE.md and is preserved unchanged.
- `AnthropicProvider` keeps the API-key client and the SDK's native
  `messages.count_tokens()` call.

Tests collapse from two duplicated `Test{Bedrock,Anthropic}Batching`
classes to one set against the mapper (`tests/test_anthropic_mapper.py`),
plus two tiny per-provider smoke tests in `tests/test_api_message_batching.py`
that verify each provider's `complete()` actually delegates to the mapper.

**Deepening choice**: deepened the LLM seam. The mapper is a deep module
(~120 lines of dense translation behind a 3-function interface) and the two
providers above it are now thin adapters. Two real adapters (Bedrock,
native Anthropic) — both in production — justify the seam.

## Consequences

**Trade-offs**
- One extra module in `agent/llm/`. The deletion test passes: removing
  `anthropic_mapper.py` makes both providers non-functional, so it isn't a
  shallow pass-through.
- `to_api_tool` always injects `"type": "object"` if missing, including for
  the native Anthropic provider. Previously the native provider passed the
  schema through verbatim. This is a no-op when the schema already declares
  `type` (idempotent), and matches the JSON Schema spec for object inputs;
  no behaviour change is expected against the live API.

**Rejected alternatives**
- *`BaseAnthropicProvider` mixin with `_build_api_messages`*: shallow
  inheritance abstraction. Inheritance doesn't make the wire-format
  knowledge any deeper than two copies did — it just hides the duplication.
- *Strategy pattern with hooks for the Bedrock schema fixup*: heavier than
  one tiny variation needs. Locality > leverage; we put the variation
  inside the mapper, not split between modules.
- *Abstract `MessageMapper` interface with multiple implementations*: only
  one wire format exists (Anthropic's; `claude_cli` is passthrough and
  bypasses tool-calling entirely). One adapter is hypothetical, two is
  real — there's no second mapper to justify the port.
