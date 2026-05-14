"""Structural backlog validator — ADR-015 §9 (scrum-points sizing).

The architect's backlog submission must pass a structural validator
before the orchestrator dispatches items. Each item must carry:

  - ``title: str`` (non-empty)
  - ``description: str`` with ≥80 whitespace-split words
  - ``justification: str`` (non-empty)
  - ``affected_routes: list[str]`` (may be empty, but field must exist)
  - ``affected_files_estimate: int`` (≥1)

The validator also runs the no-defer text layer (§8 layer 2): a regex
sweep over title + description + justification for forbidden phrases
(``raise NotImplementedError``, ``# TODO(phase``, ``Phase 1``, ``v2 ships``,
``will be implemented later``, ``for now this is a stub``, ``# Phase-\\d``
(the hyphen variant, per Phase 4 lesson), ``# Phase \\d``).

A failure returns a :class:`ValidationResult` with ``ok=False`` plus
structured :class:`Rejection` rows the architect's next turn can act on.
The ``allow-stub`` opt-out applies to **code only** — it does not exempt
backlog item text.
"""

from __future__ import annotations

import pytest


def _good_item(**overrides: object) -> dict:
    """An item that passes every structural check by default.

    Description is intentionally exactly 80 words so individual tests can
    override fields without bumping into the size floor accidentally.
    """

    description = " ".join(["word"] * 80)
    base = {
        "title": "Add auth route",
        "description": description,
        "justification": "Auth is gating every subsequent slice and needs its own PR.",
        "affected_routes": ["/api/auth/login"],
        "affected_files_estimate": 4,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_item_passes() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    result = validate_backlog([_good_item()])
    assert result.ok, f"expected ok, got rejections: {result.rejections}"
    assert result.rejections == []


def test_multiple_valid_items_pass() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    result = validate_backlog([_good_item(), _good_item(title="Add cart")])
    assert result.ok
    assert result.rejections == []


# ---------------------------------------------------------------------------
# Structural rejections
# ---------------------------------------------------------------------------


def test_missing_description_rejected() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    item = _good_item()
    del item["description"]
    result = validate_backlog([item])

    assert not result.ok
    fields = {r.field for r in result.rejections}
    assert "description" in fields
    assert all(r.item_index == 0 for r in result.rejections)


def test_missing_title_rejected() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    item = _good_item(title="")
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "title" for r in result.rejections)


def test_missing_justification_rejected() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    item = _good_item(justification="")
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "justification" for r in result.rejections)


def test_missing_affected_routes_field_rejected() -> None:
    """The field must EXIST even if it's an empty list."""

    from agent.lifecycle.trio.validators import validate_backlog

    item = _good_item()
    del item["affected_routes"]
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "affected_routes" for r in result.rejections)


def test_empty_affected_routes_list_is_allowed() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    item = _good_item(affected_routes=[])
    result = validate_backlog([item])

    assert result.ok, f"empty list should be allowed: {result.rejections}"


def test_affected_files_estimate_must_be_positive_int() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    item = _good_item(affected_files_estimate=0)
    result = validate_backlog([item])
    assert not result.ok
    assert any(r.field == "affected_files_estimate" for r in result.rejections)

    item = _good_item(affected_files_estimate="four")
    result = validate_backlog([item])
    assert not result.ok
    assert any(r.field == "affected_files_estimate" for r in result.rejections)


# ---------------------------------------------------------------------------
# Description word-count floor (ADR-015 §9 — scrum-points framing).
# ---------------------------------------------------------------------------


def test_description_below_80_words_rejected() -> None:
    """79 words must fail — the floor is ≥80."""

    from agent.lifecycle.trio.validators import validate_backlog

    short_desc = " ".join(["word"] * 79)
    item = _good_item(description=short_desc)
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "description" for r in result.rejections)


def test_description_at_80_words_passes() -> None:
    """Exactly 80 words is the boundary — passes."""

    from agent.lifecycle.trio.validators import validate_backlog

    desc = " ".join(["word"] * 80)
    item = _good_item(description=desc)
    result = validate_backlog([item])

    assert result.ok, f"80 words should pass: {result.rejections}"


# ---------------------------------------------------------------------------
# No-defer layer 2 — text in title / description / justification.
# ---------------------------------------------------------------------------


def test_phase_1_in_description_rejected() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    desc = " ".join(["word"] * 80) + " Phase 1 will fill this in later"
    item = _good_item(description=desc)
    result = validate_backlog([item])

    assert not result.ok
    # The no-defer rejection's field should point at description so the
    # architect knows which field to fix.
    assert any(r.field == "description" for r in result.rejections)


def test_phase_hyphen_variant_in_description_rejected() -> None:
    """The hyphenated ``# Phase-7`` variant must be caught (Phase 4 lesson)."""

    from agent.lifecycle.trio.validators import validate_backlog

    desc = " ".join(["word"] * 80) + " The stub will land in # Phase-7 follow-up"
    item = _good_item(description=desc)
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "description" for r in result.rejections)


def test_will_be_implemented_later_rejected() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    desc = " ".join(["word"] * 80) + " details will be implemented later"
    item = _good_item(description=desc)
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "description" for r in result.rejections)


def test_raise_notimplementederror_in_description_rejected() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    desc = " ".join(["word"] * 80) + " the handler will raise NotImplementedError until ready"
    item = _good_item(description=desc)
    result = validate_backlog([item])

    assert not result.ok


def test_todo_phase_in_description_rejected() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    desc = " ".join(["word"] * 80) + " leave a # TODO(phase 2) marker for the follow-up"
    item = _good_item(description=desc)
    result = validate_backlog([item])

    assert not result.ok


def test_v2_ships_in_justification_rejected() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    item = _good_item(justification="Slice it now; v2 ships the polish later.")
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "justification" for r in result.rejections)


def test_for_now_this_is_a_stub_in_title_rejected() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    item = _good_item(title="auth — for now this is a stub")
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "title" for r in result.rejections)


def test_lowercase_phase_variant_in_description_rejected() -> None:
    """``# phase 2`` (lowercase) must be flagged identically to ``# Phase 2``."""

    from agent.lifecycle.trio.validators import validate_backlog

    desc = " ".join(["word"] * 80) + " leave a # phase 2 marker"
    item = _good_item(description=desc)
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "description" for r in result.rejections)


def test_lowercase_phase_hyphen_variant_in_description_rejected() -> None:
    """``# phase-3`` (lowercase hyphen) must be flagged."""

    from agent.lifecycle.trio.validators import validate_backlog

    desc = " ".join(["word"] * 80) + " add # phase-3 marker"
    item = _good_item(description=desc)
    result = validate_backlog([item])

    assert not result.ok
    assert any(r.field == "description" for r in result.rejections)


def test_phase_colon_variant_in_description_rejected() -> None:
    """``# Phase 2:`` (colon suffix) and ``# Phase-2:`` must be flagged."""

    from agent.lifecycle.trio.validators import validate_backlog

    desc_colon = " ".join(["word"] * 80) + " # Phase 2: implement later"
    desc_hyphen_colon = " ".join(["word"] * 80) + " # Phase-2: follow-up"

    for desc in (desc_colon, desc_hyphen_colon):
        result = validate_backlog([_good_item(description=desc)])
        assert not result.ok, f"expected reject for {desc!r}"
        assert any(r.field == "description" for r in result.rejections)


def test_allow_stub_optout_does_not_exempt_backlog_text() -> None:
    """``# auto-agent: allow-stub`` is a code-line opt-out only.

    In backlog item text it must still fail — the optout suffix doesn't
    rescue the "phase 1" / NotImplementedError text itself.
    """

    from agent.lifecycle.trio.validators import validate_backlog

    desc = " ".join(["word"] * 80) + " Phase 1 fills this in  # auto-agent: allow-stub"
    item = _good_item(description=desc)
    result = validate_backlog([item])

    assert not result.ok, "allow-stub must not exempt backlog text from the no-defer rule"


# ---------------------------------------------------------------------------
# Rejection shape
# ---------------------------------------------------------------------------


def test_rejection_carries_index_field_and_reason() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    items = [
        _good_item(),  # index 0 — passes
        _good_item(description="too short"),  # index 1 — fails on words
    ]
    result = validate_backlog(items)

    assert not result.ok
    bad = [r for r in result.rejections if r.item_index == 1]
    assert bad, "expected at least one rejection on index 1"
    r = bad[0]
    assert r.field == "description"
    assert isinstance(r.reason, str) and r.reason


def test_top_level_items_must_be_list() -> None:
    from agent.lifecycle.trio.validators import validate_backlog

    result = validate_backlog("not a list")  # type: ignore[arg-type]
    assert not result.ok


def test_empty_backlog_is_rejected() -> None:
    """A submitted backlog must contain at least one item."""

    from agent.lifecycle.trio.validators import validate_backlog

    result = validate_backlog([])
    assert not result.ok


# ---------------------------------------------------------------------------
# ValidationResult / Rejection import shape.
# ---------------------------------------------------------------------------


def test_validation_result_and_rejection_are_importable() -> None:
    from agent.lifecycle.trio.validators import Rejection, ValidationResult

    r = ValidationResult(ok=True, rejections=[])
    assert r.ok is True
    rej = Rejection(item_index=2, field="description", reason="too short")
    assert rej.item_index == 2
    assert rej.field == "description"
    assert rej.reason == "too short"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
