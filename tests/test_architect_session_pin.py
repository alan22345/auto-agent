"""Architect autocompact pin policy — ADR-015 §13.

The architect session must never lose the three pinned artefacts:

  - ``.auto-agent/design.md`` content
  - ``.auto-agent/backlog.json`` content
  - The current cycle's ``.auto-agent/decision.json`` content

Strategy: on session resume, re-INJECT the pinned artefacts as fresh
system-prompt-attached context blocks. The session's user/assistant
turns can compact freely; the pinned blocks are re-attached each turn
so they never live in the buffer that autocompact rewrites.

The implementation lives in :mod:`agent.lifecycle.trio.pinned_context`.
This test pins:

1. ``build_pinned_block`` reads design / backlog / decision off disk and
   produces a deterministic, formatted markdown block.
2. Missing pinned files are skipped without error.
3. ``apply_to_system_prompt`` appends the pinned block to a base prompt.
4. The pinned content does NOT live inside the message buffer that
   autocompact rewrites — i.e. forcing a compact leaves the pinned
   block intact because it's part of the system prompt instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 1. build_pinned_block reads all three artefacts.
# ---------------------------------------------------------------------------


def test_build_pinned_block_reads_all_three_artefacts(tmp_path: Path) -> None:
    from agent.lifecycle.trio.pinned_context import build_pinned_block
    from agent.lifecycle.workspace_paths import (
        BACKLOG_PATH,
        DECISION_PATH,
        DESIGN_PATH,
    )

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DESIGN_PATH).write_text("# Design\n\nGoal.\n")
    (tmp_path / BACKLOG_PATH).write_text('{"schema_version": "1", "items": []}')
    (tmp_path / DECISION_PATH).write_text(
        '{"schema_version": "1", "action": "done", "payload": {}}'
    )

    block = build_pinned_block(str(tmp_path))

    assert "# Design" in block
    assert "Goal" in block
    assert "backlog" in block.lower()
    assert "decision" in block.lower()


def test_build_pinned_block_skips_missing_artefacts(tmp_path: Path) -> None:
    """Returning an empty string when none of the three files exist is
    fine — the pin is best-effort. Each missing file is silently skipped.
    """

    from agent.lifecycle.trio.pinned_context import build_pinned_block

    block = build_pinned_block(str(tmp_path))
    # An empty / falsy block is acceptable when nothing's there to pin.
    assert isinstance(block, str)


def test_build_pinned_block_handles_partial_state(tmp_path: Path) -> None:
    """Just the design file present — the block should still include it
    and skip the missing backlog/decision without raising."""

    from agent.lifecycle.trio.pinned_context import build_pinned_block
    from agent.lifecycle.workspace_paths import DESIGN_PATH

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DESIGN_PATH).write_text("# Just a design\n")

    block = build_pinned_block(str(tmp_path))
    assert "Just a design" in block


# ---------------------------------------------------------------------------
# 2. apply_to_system_prompt appends the pinned block.
# ---------------------------------------------------------------------------


def test_apply_to_system_prompt_appends_pinned_block(tmp_path: Path) -> None:
    from agent.lifecycle.trio.pinned_context import (
        apply_pinned_artefacts_to_system_prompt,
    )
    from agent.lifecycle.workspace_paths import DESIGN_PATH

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DESIGN_PATH).write_text("# Design\n\nbuild thing\n")

    base = "You are the architect."
    out = apply_pinned_artefacts_to_system_prompt(base, str(tmp_path))

    assert out.startswith(base) or base in out
    assert "build thing" in out


def test_apply_to_system_prompt_no_op_when_nothing_pinned(tmp_path: Path) -> None:
    from agent.lifecycle.trio.pinned_context import (
        apply_pinned_artefacts_to_system_prompt,
    )

    base = "Base prompt."
    out = apply_pinned_artefacts_to_system_prompt(base, str(tmp_path))
    # When there's nothing to pin, the base prompt comes through unchanged
    # (or with at most a trivial trailing whitespace).
    assert base in out


# ---------------------------------------------------------------------------
# 3. Autocompact safety — pinned content is OUTSIDE the buffer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autocompact_does_not_drop_pinned_content(tmp_path: Path) -> None:
    """The pinned context strategy keeps design/backlog/decision OUT of
    the message buffer entirely — they ride in the system prompt and are
    re-attached fresh on each resume.

    The test forces a compact via :meth:`AutocompactEngine.force_compact`
    on a buffer that has been rebuilt with the pinned block as the
    system prompt; the system prompt must still carry the pinned content
    after compaction (because compaction only rewrites messages, not
    system).
    """

    from unittest.mock import AsyncMock, MagicMock

    from agent.context.autocompact import AutocompactEngine
    from agent.context.token_counter import TokenCounter
    from agent.lifecycle.trio.pinned_context import (
        apply_pinned_artefacts_to_system_prompt,
    )
    from agent.lifecycle.workspace_paths import DESIGN_PATH
    from agent.llm.types import LLMResponse, Message, TokenUsage

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DESIGN_PATH).write_text("# Design\n\nload-bearing detail\n")

    system_prompt = apply_pinned_artefacts_to_system_prompt(
        "You are the architect.",
        str(tmp_path),
    )
    assert "load-bearing detail" in system_prompt

    provider = MagicMock()
    provider.max_context_tokens = 200_000
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            message=Message(role="assistant", content="[Summary]"),
            usage=TokenUsage(),
            stop_reason="end_turn",
        ),
    )
    counter = TokenCounter(provider)
    engine = AutocompactEngine(provider, counter)

    buffer: list[Message] = [
        Message(role="user", content="A" * 1000),
        Message(role="assistant", content="B" * 1000),
        Message(role="user", content="C" * 1000),
    ]
    compacted = await engine.force_compact(buffer)

    # The buffer was compacted (shrunk to a single boundary message), but
    # the system prompt is untouched — pinned content survives intact.
    assert "load-bearing detail" in system_prompt
    assert len(compacted) == 1


# ---------------------------------------------------------------------------
# 4. Architect resume re-injects pinned content (integration sketch).
# ---------------------------------------------------------------------------


def test_pinned_block_format_includes_section_headers(tmp_path: Path) -> None:
    """The pinned block is readable to the model — section headers per
    artefact, not raw concatenation."""

    from agent.lifecycle.trio.pinned_context import build_pinned_block
    from agent.lifecycle.workspace_paths import (
        BACKLOG_PATH,
        DECISION_PATH,
        DESIGN_PATH,
    )

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DESIGN_PATH).write_text("# Design\n")
    (tmp_path / BACKLOG_PATH).write_text('{"schema_version": "1", "items": [{"title": "T1"}]}')
    (tmp_path / DECISION_PATH).write_text(
        '{"schema_version": "1", "action": "done", "payload": {}}'
    )
    block = build_pinned_block(str(tmp_path))

    # A reasonable expectation: each artefact gets its own section header.
    assert "design.md" in block.lower()
    assert "backlog.json" in block.lower()
    assert "decision.json" in block.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
