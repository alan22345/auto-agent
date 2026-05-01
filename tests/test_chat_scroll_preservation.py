"""Regression test for the legacy ``web/`` UI's chat-area scroll behavior.

``renderChat`` used to set ``el.scrollTop = el.scrollHeight`` unconditionally
on every WebSocket re-render, which yanked the user back to the bottom while
they were scrolled up reading prior messages, plan output, or an error. The
fix gates the scroll-to-bottom on ``wasAtBottom`` (with an 80px threshold)
plus a small set of forced-scroll cases (task switch, first paint, user just
sent), and surfaces a floating ``↓ N new messages`` pill when content arrives
while the user is reading history.

Static-grep test in the same spirit as ``tests/test_ws_refresh.py`` and
``tests/test_suggestion_expand.py``: behavioural tests cannot exercise the
rendered HTML/scroll state of a static SPA, so we read the source file and
assert the load-bearing markers are present (or absent, for the regressed
unconditional-scroll line).
"""
from __future__ import annotations

from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "web" / "static" / "index.html"
CHAT_AREA_TSX = (
    Path(__file__).resolve().parent.parent
    / "web-next"
    / "components"
    / "chat"
    / "chat-area.tsx"
)


def _read_index() -> str:
    return INDEX_HTML.read_text()


def _read_chat_area_tsx() -> str:
    return CHAT_AREA_TSX.read_text()


def test_render_chat_does_not_force_scroll_to_bottom():
    """``renderChat`` must NOT contain the unconditional bottom-snap line.

    Reintroducing ``el.scrollTop = el.scrollHeight`` as the last statement of
    ``renderChat`` re-creates the original bug: every agent_stream WS event
    yanks the user back to the bottom while they are reading history.
    """
    text = _read_index()

    marker = "function renderChat()"
    start = text.find(marker)
    assert start != -1, "could not locate renderChat() in web/static/index.html"
    # Scope to renderChat's body — find the next top-level function declaration.
    end = text.find("\n    function ", start + len(marker))
    assert end != -1, "could not locate end of renderChat() body"
    body = text[start:end]

    # The scroll-to-bottom in renderChat must always be conditional. The bug
    # was an unconditional final ``el.scrollTop = el.scrollHeight;`` that ran
    # on every WS message. Allow conditional uses (inside if-branches) but
    # forbid the trailing standalone line.
    assert "el.scrollTop = el.scrollHeight;\n    }" not in body, (
        "renderChat() ends with an unconditional el.scrollTop = el.scrollHeight; "
        "this is the original Groundhog-Day-of-scroll bug — gate it on "
        "wasAtBottom or one of the forced-scroll cases."
    )


def test_render_chat_captures_was_at_bottom_before_innerhtml_rewrite():
    """``renderChat`` must capture ``wasAtBottom`` before rewriting innerHTML.

    Order matters: ``isAtBottom`` reads ``scrollHeight``/``scrollTop``, both of
    which are reset by the innerHTML rewrite. Capturing after the rewrite
    would always report ``wasAtBottom = true`` and re-create the bug.
    """
    text = _read_index()

    marker = "function renderChat()"
    start = text.find(marker)
    assert start != -1
    body = text[start : start + 4000]

    capture_idx = body.find("isAtBottom(el)")
    rewrite_idx = body.find("el.innerHTML = prefixHtml")
    assert capture_idx != -1, "renderChat() no longer captures isAtBottom(el)"
    assert rewrite_idx != -1, "renderChat() no longer rewrites el.innerHTML"
    assert capture_idx < rewrite_idx, (
        "isAtBottom(el) must be captured BEFORE el.innerHTML is rewritten — "
        "the rewrite resets scrollTop, so capturing after always reports "
        "wasAtBottom=true and the bug returns."
    )


def test_new_message_pill_dom_and_state_present():
    """The pill markup, its CSS wrapper, and the module-level state must exist.

    The pill is anchored bottom-right of ``.chat-area-wrap`` (position:
    relative). Without that wrapper, the absolutely-positioned pill ends up
    relative to the wrong ancestor. ``pendingNewMsgCount`` lives at module
    scope so it survives the WS-driven re-renders that replace innerHTML.
    """
    text = _read_index()

    assert 'class="chat-area-wrap"' in text, (
        ".chat-area-wrap container is missing — the new-msg-pill anchors to "
        "this wrapper via position:absolute and won't sit in the right place "
        "without it."
    )
    assert 'id="new-msg-pill"' in text, "new-msg-pill button is missing"
    assert 'id="new-msg-count"' in text, "new-msg-count span is missing"
    assert "let pendingNewMsgCount = 0" in text, (
        "module-level pendingNewMsgCount counter is missing — without it, the "
        "pill count would not survive WS-driven renderChat() calls."
    )
    assert "lastRenderedMsgCountByKey" in text, (
        "lastRenderedMsgCountByKey bookkeeping is missing — needed to compute "
        "newDelta between renders without double-counting."
    )


def test_pill_clears_on_manual_rescroll_to_bottom():
    """A scroll listener on #chat-area must clear the pill at the bottom band.

    Without it, the user can scroll back to the bottom manually, miss the pill
    button, and be left with a stale ``↓ N new messages`` indicator that no
    longer reflects unread content.
    """
    text = _read_index()

    # The DOMContentLoaded init block wires the scroll listener. The literal
    # ``DOMContentLoaded`` string appears earlier in a comment, so search for
    # the actual ``addEventListener('DOMContentLoaded'`` call.
    init_idx = text.find("addEventListener('DOMContentLoaded'")
    assert init_idx != -1, "could not locate DOMContentLoaded handler"
    init_block = text[init_idx : init_idx + 800]

    assert 'document.getElementById("chat-area")' in init_block, (
        "DOMContentLoaded init must obtain a reference to #chat-area to wire "
        "the scroll listener that clears the new-msg-pill."
    )
    assert 'addEventListener("scroll"' in init_block, (
        "DOMContentLoaded init must attach a scroll listener to #chat-area"
    )
    assert "clearNewMsgPill()" in init_block, (
        "scroll listener must call clearNewMsgPill() when the user reaches "
        "the bottom band; otherwise the pill goes stale."
    )


def test_task_delete_prunes_msg_count_bookkeeping():
    """``deleteTask`` must drop the per-task render bookkeeping entry.

    ``lastRenderedMsgCountByKey`` is a plain object dict keyed by task id and
    grows monotonically until pruned. ``taskDetailPlanExpanded`` and
    ``chatMessages`` are pruned at delete time; this entry must follow the
    same pattern or it leaks one entry per deleted task.
    """
    text = _read_index()

    marker = "function deleteTask()"
    start = text.find(marker)
    assert start != -1, "could not locate deleteTask() in web/static/index.html"
    end = text.find("\n    function ", start + len(marker))
    assert end != -1
    body = text[start:end]

    assert "delete lastRenderedMsgCountByKey[taskId]" in body, (
        "deleteTask() must prune lastRenderedMsgCountByKey[taskId] — the dict "
        "is keyed by task id and otherwise leaks one entry per deleted task."
    )


def test_web_next_chat_area_uses_conditional_scroll():
    """The web-next ChatArea must not unconditionally scroll on entries change.

    The legacy bug existed identically in
    ``web-next/components/chat/chat-area.tsx`` — a useEffect that called
    ``ref.current.scrollTo({ top: scrollHeight })`` on every entries change,
    yanking the user back to the bottom. The fix gates the scroll on
    ``wasAtBottom`` (or a forced-scroll case) and surfaces a pendingCount pill.

    Asserts the load-bearing fragments exist; if a future refactor removes
    them without preserving the behaviour, this test fires.
    """
    text = _read_chat_area_tsx()

    assert "isAtBottom" in text, (
        "isAtBottom helper is missing from chat-area.tsx — without it, the "
        "scroll preservation reverts to the unconditional scrollTo bug."
    )
    assert "pendingCount" in text, (
        "pendingCount state is missing — the new-message pill cannot count "
        "without it."
    )
    assert "wasAtBottom" in text, (
        "wasAtBottom branch is missing — every entries change would scroll "
        "to bottom unconditionally."
    )
    # The pill render block.
    assert "pendingCount > 0" in text, (
        "pendingCount > 0 conditional render of the pill is missing"
    )
