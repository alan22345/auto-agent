"""Sub-architect slice workspace layout — ADR-015 §9 / §12 / Phase 8.

The sub-architect dispatcher operates on a per-slice namespace under
``.auto-agent/slices/<name>/``. This module pins the path composition
so future slice-scoped artefacts (review files, decision file) round
trip through one set of helpers — no inline string-building in the
dispatcher or skill markdown.

The helpers under test live in
``agent.lifecycle.workspace_paths``; the dispatcher in
``agent.lifecycle.trio.sub_architect`` consumes them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Slice paths compose under the slice's own namespace, never collide
#    with the root .auto-agent/ surface.
# ---------------------------------------------------------------------------


def test_slice_dir_is_scoped_under_auto_agent() -> None:
    from agent.lifecycle.workspace_paths import AUTO_AGENT_DIR, slice_dir

    assert slice_dir("auth") == f"{AUTO_AGENT_DIR}/slices/auth"
    assert slice_dir("checkout") == f"{AUTO_AGENT_DIR}/slices/checkout"


def test_slice_design_and_backlog_paths_are_under_slice_dir() -> None:
    from agent.lifecycle.workspace_paths import (
        slice_backlog_path,
        slice_design_path,
        slice_dir,
    )

    base = slice_dir("auth")
    assert slice_design_path("auth") == f"{base}/design.md"
    assert slice_backlog_path("auth") == f"{base}/backlog.json"


def test_slice_decision_path_is_under_slice_namespace() -> None:
    """Sub-architects emit decisions to their own slice's decision file,
    not the root one — guarantees 1-level recursion bound enforcement
    can read the slice-scoped file without crossing namespaces."""
    from agent.lifecycle.workspace_paths import (
        DECISION_PATH,
        slice_decision_path,
        slice_dir,
    )

    assert slice_decision_path("auth") == f"{slice_dir('auth')}/decision.json"
    assert slice_decision_path("auth") != DECISION_PATH, (
        "slice decision must not collide with the root decision path"
    )


def test_slice_review_paths_use_slice_namespace() -> None:
    from agent.lifecycle.workspace_paths import (
        review_path,
        slice_review_path,
        slice_reviews_dir,
    )

    assert slice_reviews_dir("auth") == ".auto-agent/slices/auth/reviews"
    assert slice_review_path("auth", "T1") == (".auto-agent/slices/auth/reviews/T1.json")
    # The root review_path() must not contain the slice name — namespace
    # isolation is mandatory.
    assert "slices" not in review_path("T1")


# ---------------------------------------------------------------------------
# 2. Slice paths are isolated from the top-level workspace surface — the
#    same item_id in two slices does not collide.
# ---------------------------------------------------------------------------


def test_two_slices_with_same_item_id_dont_collide(tmp_path: Path) -> None:
    from agent.lifecycle.workspace_paths import slice_review_path

    a = tmp_path / slice_review_path("auth", "T1")
    b = tmp_path / slice_review_path("checkout", "T1")
    assert a != b


def test_grill_relay_paths_are_slice_scoped() -> None:
    """The parent-grill-relay reads / writes paths inside the slice
    that asked the question. The helpers must reflect that namespace."""
    from agent.lifecycle.workspace_paths import (
        slice_dir,
        slice_grill_answer_path,
        slice_grill_question_path,
    )

    for name in ("auth", "checkout-flow", "search_v2"):
        assert slice_grill_question_path(name).startswith(slice_dir(name))
        assert slice_grill_answer_path(name).startswith(slice_dir(name))
        assert slice_grill_question_path(name) != slice_grill_answer_path(name)
