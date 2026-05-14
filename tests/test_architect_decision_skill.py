"""Architect decision via skills — ADR-015 §2 / §9 / §12 / Phase 6.

The trio architect's per-cycle decision is now written via the
``submit-architect-decision`` skill to ``.auto-agent/decision.json``.
The orchestrator reads the file after ``agent.run`` returns. The five
valid actions: ``done``, ``dispatch_new``, ``escalate``,
``spawn_sub_architects``, ``awaiting_clarification``.

If ``decision.json`` is missing after the architect run, the orchestrator
falls back to the ADR-014 Haiku extractor as a resilience net (the
extractor stays in place during the transition window).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. read_decision recognises each of the five action types.
# ---------------------------------------------------------------------------


_VALID_ACTIONS = (
    "done",
    "dispatch_new",
    "escalate",
    "spawn_sub_architects",
    "awaiting_clarification",
)


@pytest.mark.parametrize("action", _VALID_ACTIONS)
def test_read_decision_accepts_all_five_actions(action: str, tmp_path: Path) -> None:
    from agent.lifecycle.trio.architect_decision import read_decision
    from agent.lifecycle.workspace_paths import DECISION_PATH

    (tmp_path / ".auto-agent").mkdir()
    payload = {"schema_version": "1", "action": action, "payload": {}}
    if action == "awaiting_clarification":
        payload["payload"] = {"question": "What stack?"}
    if action == "escalate":
        payload["payload"] = {"reason": "unsolvable"}
    if action == "spawn_sub_architects":
        payload["payload"] = {"slices": [{"name": "auth", "scope": "everything auth"}]}
    if action == "dispatch_new":
        payload["payload"] = {"items": [{"title": "x", "description": "y"}]}
    (tmp_path / DECISION_PATH).write_text(json.dumps(payload))

    decision = read_decision(str(tmp_path))
    assert decision is not None
    assert decision["action"] == action


def test_read_decision_returns_none_when_file_missing(tmp_path: Path) -> None:
    from agent.lifecycle.trio.architect_decision import read_decision

    assert read_decision(str(tmp_path)) is None


def test_read_decision_rejects_unknown_action(tmp_path: Path) -> None:
    from agent.lifecycle.trio.architect_decision import read_decision
    from agent.lifecycle.workspace_paths import DECISION_PATH

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DECISION_PATH).write_text(
        json.dumps({"schema_version": "1", "action": "weird", "payload": {}})
    )

    assert read_decision(str(tmp_path)) is None


def test_read_decision_rejects_wrong_schema_version(tmp_path: Path) -> None:
    from agent.lifecycle.trio.architect_decision import read_decision
    from agent.lifecycle.workspace_paths import DECISION_PATH

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DECISION_PATH).write_text(
        json.dumps({"schema_version": "999", "action": "done", "payload": {}})
    )

    with pytest.raises(ValueError):
        read_decision(str(tmp_path))


# ---------------------------------------------------------------------------
# 2. spawn_sub_architects payload shape validation.
# ---------------------------------------------------------------------------


def test_spawn_sub_architects_payload_requires_slices(tmp_path: Path) -> None:
    """The ``slices`` list must be present and each slice must carry
    ``name`` and ``scope``."""

    from agent.lifecycle.trio.architect_decision import read_decision
    from agent.lifecycle.workspace_paths import DECISION_PATH

    (tmp_path / ".auto-agent").mkdir()
    # No slices.
    (tmp_path / DECISION_PATH).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "action": "spawn_sub_architects",
                "payload": {},
            }
        )
    )
    assert read_decision(str(tmp_path)) is None

    # Empty slices.
    (tmp_path / DECISION_PATH).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "action": "spawn_sub_architects",
                "payload": {"slices": []},
            }
        )
    )
    assert read_decision(str(tmp_path)) is None

    # Slice missing scope.
    (tmp_path / DECISION_PATH).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "action": "spawn_sub_architects",
                "payload": {"slices": [{"name": "auth"}]},
            }
        )
    )
    assert read_decision(str(tmp_path)) is None

    # Good shape.
    (tmp_path / DECISION_PATH).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "action": "spawn_sub_architects",
                "payload": {
                    "slices": [
                        {"name": "auth", "scope": "everything auth"},
                        {"name": "cart", "scope": "checkout flow"},
                    ],
                },
            }
        )
    )
    decision = read_decision(str(tmp_path))
    assert decision is not None
    assert decision["action"] == "spawn_sub_architects"
    slices = decision["payload"]["slices"]
    assert len(slices) == 2
    assert slices[0]["name"] == "auth"
    assert slices[0]["scope"] == "everything auth"


# ---------------------------------------------------------------------------
# 3. resolve_decision: file-first, then Haiku extractor fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_decision_prefers_decision_json(tmp_path: Path) -> None:
    """If decision.json is present, the orchestrator uses it and never
    invokes the legacy Haiku extractor."""

    from agent.lifecycle.trio import architect_decision as ad
    from agent.lifecycle.workspace_paths import DECISION_PATH

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DECISION_PATH).write_text(
        json.dumps({"schema_version": "1", "action": "done", "payload": {}})
    )

    extractor = AsyncMock()

    with patch.object(ad, "extract_checkpoint_output", new=extractor):
        decision = await ad.resolve_decision(
            workspace=str(tmp_path),
            prose_output="(ignored)",
        )

    assert decision is not None
    assert decision["action"] == "done"
    extractor.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_decision_falls_back_to_haiku_when_file_missing(
    tmp_path: Path,
) -> None:
    """When decision.json is missing, the orchestrator falls back to the
    ADR-014 Haiku extractor on the prose output."""

    from agent.lifecycle.trio import architect_decision as ad

    extractor = AsyncMock(
        return_value={
            "decision": {"action": "continue", "reason": "ok"},
            "backlog": None,
        }
    )

    with patch.object(ad, "extract_checkpoint_output", new=extractor):
        decision = await ad.resolve_decision(
            workspace=str(tmp_path),
            prose_output="Decision: continue — everything is fine.",
        )

    assert decision is not None
    extractor.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_decision_returns_none_when_both_paths_fail(
    tmp_path: Path,
) -> None:
    from agent.lifecycle.trio import architect_decision as ad

    extractor = AsyncMock(return_value=None)

    with patch.object(ad, "extract_checkpoint_output", new=extractor):
        decision = await ad.resolve_decision(
            workspace=str(tmp_path),
            prose_output="...",
        )

    assert decision is None


# ---------------------------------------------------------------------------
# 4. SKILL.md surface — the spawn_sub_architects action is documented.
# ---------------------------------------------------------------------------


def test_submit_architect_decision_skill_documents_spawn() -> None:
    """The SKILL.md for ``submit-architect-decision`` must mention each
    of the five actions, including ``spawn_sub_architects``, so CC knows
    the option exists when designing a huge task."""

    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    skill_md = repo_root / "skills" / "auto-agent" / "submit-architect-decision" / "SKILL.md"
    text = skill_md.read_text()

    for action in _VALID_ACTIONS:
        assert action in text, f"submit-architect-decision SKILL.md missing action {action!r}"

    # The slices shape is documented inline.
    assert "slices" in text
    assert "scope" in text


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
