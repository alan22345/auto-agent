"""Regression test for the legacy ``web/`` UI's suggestion expand affordance.

PO-generated suggestion cards used to render only the title and a single
``rationale || description`` line, hiding multi-paragraph rationales from
reviewers before they approved or rejected. The fix in
``renderRepoDetailSuggestions`` (web/static/index.html) makes each card
click-to-expand, with full description and rationale shown in a
``.ff-sug-expanded`` block.

This is a static-grep test in the same spirit as
``tests/test_ws_refresh.py``: behavioural tests cannot reach the rendered
HTML in a static SPA, so we read the source file and assert the relevant
markers are present (or absent, for the dead ``id=`` attributes that were
removed).
"""
from __future__ import annotations

from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "web" / "static" / "index.html"


def _read_index() -> str:
    return INDEX_HTML.read_text()


def test_dead_suggestion_button_ids_removed():
    """The ``sug-approve-${id}`` / ``sug-reject-${id}`` ids are never queried.

    They were dead weight on every render; the suggestion-expand task removed
    them. Re-introducing them would suggest a future change is targeting them
    via querySelector — which would also be dead, so flag it.
    """
    text = _read_index()
    assert 'id="sug-approve-' not in text, (
        "dead id='sug-approve-${s.id}' attribute reintroduced on suggestion "
        "Approve button — nothing queries it; remove."
    )
    assert 'id="sug-reject-' not in text, (
        "dead id='sug-reject-${s.id}' attribute reintroduced on suggestion "
        "Reject button — nothing queries it; remove."
    )


def test_suggestion_expand_state_and_toggle_present():
    """The per-card expand state Set and its toggle helper must exist.

    Mirrors the ``taskDetailPlanExpanded`` pattern: a module-level container
    keyed by row id so the open/closed state survives the WS-driven
    ``suggestion_list`` re-renders that replace innerHTML wholesale.
    """
    text = _read_index()
    assert "suggestionExpanded = new Set()" in text, (
        "module-level suggestionExpanded Set is missing — without it, the "
        "expand state lives only in DOM and gets wiped on every WS-driven "
        "renderRepoDetailSuggestions() call."
    )
    assert "function toggleSuggestionExpanded(" in text, (
        "toggleSuggestionExpanded helper is missing"
    )


def test_suggestion_card_click_toggles_expand_but_actions_do_not():
    """The card click toggles expand; clicks on Approve/Reject must not.

    The ``.ff-sug-actions`` wrapper has ``onclick="event.stopPropagation()"``
    so button clicks don't bubble to the card-level toggle. If a future
    refactor drops that, the card flips open/closed every time a user tries
    to approve a suggestion — confusing and breaks the modal flow.
    """
    text = _read_index()

    # Card-level toggle handler.
    assert 'onclick="toggleSuggestionExpanded(' in text, (
        "renderRepoDetailSuggestions must wire the card to toggleSuggestionExpanded"
    )

    # Locate the actions row inside the suggestion card and assert it
    # stops propagation. Search the rendered template literal block.
    marker = 'class="ff-sug-actions"'
    idx = text.find(marker)
    assert idx != -1, "could not locate ff-sug-actions wrapper"
    block = text[idx : idx + 200]
    assert "event.stopPropagation()" in block, (
        "ff-sug-actions wrapper must call event.stopPropagation() so clicking "
        "Approve or Reject does not also toggle the card expand state"
    )


def test_suggestion_expanded_block_renders_full_text_safely():
    """The expanded block shows full description + rationale, escaped.

    Both fields are user-rendered text (PO-generated) but they reach the DOM
    via interpolation, so they MUST be passed through ``escHtml()`` to stay
    XSS-safe. Asserts the expanded block exists and that both fields go
    through ``escHtml()``.
    """
    text = _read_index()

    # The expanded-block class must be rendered somewhere in the script.
    assert 'class="ff-sug-expanded"' in text, (
        "ff-sug-expanded block markup missing from renderRepoDetailSuggestions"
    )

    # Both fields must be escaped — not interpolated raw.
    assert "escHtml(s.description" in text, (
        "s.description must be passed through escHtml() in the expanded block"
    )
    assert "escHtml(s.rationale" in text, (
        "s.rationale must be passed through escHtml() in the expanded block"
    )
