"""Regression tests for the gap-fix bug where the architect emitted
``dispatch_new`` items without ``id`` fields and the tiebreak then
false-accepted them on the next no-diff round.

Observed on harpoon #25 (2026-05-24): the gap-fix architect emitted 13
items to build the funnel module. Items 11-23 had no ``id`` so the
tiebreak prompt rendered as ``## Tiebreak — work item : <title>`` with
no item handle. The architect, asked to decide on a transcript with no
item-specific anchor, latched onto the most recent commit as "evidence
of done" and returned ``accept`` for every funnel item even though
``src/harpoon/funnel/`` was empty. Four false-accepts in a row before
the run was halted.

Two regression hooks here:

1. ``_assign_missing_ids`` — pure helper that augments architect-emitted
   items with auto-generated IDs (``G1``, ``G2``, ...) when the field is
   missing or empty. Called by ``_append_backlog_items``.

2. ``architect_tiebreak`` post-hoc verification — when ``no_diff_mode``
   and the architect returns ``accept``, parse the work item description
   for file paths and verify at least one exists on disk. If the
   description names paths but NONE exist, override to
   ``revise_backlog`` because the architect cannot have known the work
   was done.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Part A — _assign_missing_ids
# ---------------------------------------------------------------------------


def test_assign_missing_ids_fills_blank_id_field():
    """Items without an ``id`` (missing, empty string, or explicit None)
    get auto-assigned IDs.

    The ``None`` case matters: the architect's submit-architect-decision
    skill writes items as JSON, and ``"id": null`` deserialises to
    ``{"id": None}`` — a key that exists with a None value. Task 28
    (2026-05-27) stalled because a backlog item appended via this path
    survived with ``id: null`` and the dispatcher then dispatched it
    with ``item_id=None`` (uuid5 of ``trio-coder-28-None``) instead of
    a healed id.
    """
    from agent.lifecycle.trio import _assign_missing_ids

    existing: list[dict] = []
    new_items = [
        {"title": "Create funnel models", "description": "..."},
        {"id": "", "title": "Create funnel repo", "description": "..."},
        {"id": None, "title": "Gap-fix item (id: null in JSON)", "description": "..."},
        {"id": "F3", "title": "Add tests", "description": "..."},
    ]
    out = _assign_missing_ids(existing, new_items)

    assert out[0]["id"] == "G1"
    assert out[1]["id"] == "G2"
    assert out[2]["id"] == "G3"
    assert out[3]["id"] == "F3"  # explicit id preserved


def test_assign_missing_ids_skips_existing_g_indices():
    """When existing backlog already has G1/G2 items, next missing id is G3."""
    from agent.lifecycle.trio import _assign_missing_ids

    existing = [
        {"id": "T1", "title": "x", "status": "done"},
        {"id": "G1", "title": "y", "status": "done"},
        {"id": "G2", "title": "z", "status": "pending"},
    ]
    new_items = [
        {"title": "new"},
        {"title": "another"},
    ]
    out = _assign_missing_ids(existing, new_items)

    assert out[0]["id"] == "G3"
    assert out[1]["id"] == "G4"


def test_assign_missing_ids_renames_colliding_explicit_ids():
    """Bug discovered on task #26 (2026-05-25, 12-hr stall): the gap-fix
    architect emitted new items with explicit ids ``G1``, ``G2`` even
    though the backlog already had a done item with ``id=G1``.
    ``_mark_item_done(id=G1)`` matched the first (done) G1, broke out
    of the loop, and the pending duplicate never got marked done.

    Fix: a NEW item with an explicit id that collides with an existing
    id gets renamed to the next free ``G{N}`` slot. Established ids
    (those already in existing-but-not-new-items) stay put."""
    from agent.lifecycle.trio import _assign_missing_ids

    existing = [
        {"id": "T1", "title": "old t", "status": "done"},
        {"id": "G1", "title": "old g1", "status": "done"},
    ]
    new_items = [
        {"id": "G1", "title": "new g1 (collision)"},
        {"id": "G2", "title": "new g2"},
        {"title": "new no-id"},
    ]
    out = _assign_missing_ids(existing, new_items)

    # Invariants: the colliding G1 was renamed, the union of existing
    # and out has no duplicates, and every output item has an id.
    out_ids = [o["id"] for o in out]
    assert out_ids[0] != "G1", out
    assert all((o.get("id") or "").strip() for o in out)
    existing_ids = {e["id"] for e in existing}
    assert existing_ids.isdisjoint(set(out_ids)) or existing_ids - set(out_ids) == existing_ids - {
        "T1",
        "G1",
    }
    # Every id ends up unique across existing + new.
    assert len(set(existing_ids | set(out_ids))) == len(existing) + len(out)


def test_assign_missing_ids_dedupes_duplicates_within_new_items():
    """If new_items itself has two items with the same id, the second
    one gets renamed. Defends against an architect emitting duplicate
    handles in a single dispatch_new."""
    from agent.lifecycle.trio import _assign_missing_ids

    out = _assign_missing_ids(
        [],
        [
            {"id": "G1", "title": "first"},
            {"id": "G1", "title": "second"},  # collides with first
            {"title": "third"},
        ],
    )
    ids = [o["id"] for o in out]
    assert ids[0] == "G1"
    assert ids[1] != "G1", ids
    assert len(set(ids)) == 3, ids  # all unique


def test_assign_missing_ids_backfill_case_dedupes_existing_duplicates():
    """``_backfill_backlog_ids`` passes the SAME list as both existing
    and new_items. In that case, a backlog with a pre-existing
    duplicate (task #26 shape: done G1 + pending G1) should heal: the
    FIRST occurrence keeps the id, the SECOND gets renamed."""
    from agent.lifecycle.trio import _assign_missing_ids

    backlog = [
        {"id": "T1", "title": "x", "status": "done"},
        {"id": "G1", "title": "first g1", "status": "done"},
        {"id": "G1", "title": "second g1 (collision)", "status": "pending"},
        {"id": "G2", "title": "second g2", "status": "pending"},
    ]
    out = _assign_missing_ids(backlog, backlog)

    # First occurrences keep their ids; duplicates get renamed.
    assert out[0]["id"] == "T1"
    assert out[1]["id"] == "G1"
    assert out[2]["id"] != "G1", out
    assert out[2]["id"] != out[3]["id"]
    assert len({o["id"] for o in out}) == 4  # all unique


def test_assign_missing_ids_is_pure_does_not_mutate_input():
    """Caller's list must not be mutated — we return a fresh list of dicts."""
    from agent.lifecycle.trio import _assign_missing_ids

    new_items = [{"title": "x"}]
    out = _assign_missing_ids([], new_items)

    assert out is not new_items
    assert out[0] is not new_items[0]
    assert "id" not in new_items[0]  # original untouched
    assert out[0]["id"] == "G1"


@pytest.mark.asyncio
async def test_append_backlog_items_assigns_ids_to_idless_items():
    """End-to-end: ``_append_backlog_items`` stamps IDs onto bare items
    before persisting them to ``trio_backlog``."""
    from agent.lifecycle.trio import _append_backlog_items

    captured_backlog: list[dict] = []

    class FakeTask:
        def __init__(self) -> None:
            self.trio_backlog: list[dict] = [{"id": "T1", "title": "x", "status": "done"}]

    fake_task = FakeTask()

    class FakeResult:
        def scalar_one(self):
            return fake_task

    class FakeSession:
        async def execute(self, *_a, **_k):
            return FakeResult()

        async def commit(self):
            captured_backlog.extend(fake_task.trio_backlog)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    from agent.lifecycle import trio as trio_mod

    with patch.object(trio_mod, "async_session", lambda: FakeSession()):
        await _append_backlog_items(
            42,
            [
                {"title": "Build funnel repo", "description": "..."},
                {"title": "Build funnel service", "description": "..."},
            ],
        )

    # Existing T1 + 2 new items with G1/G2 IDs.
    assert len(captured_backlog) == 3
    assert captured_backlog[0]["id"] == "T1"
    assert captured_backlog[1]["id"] == "G1"
    assert captured_backlog[1]["status"] == "pending"
    assert captured_backlog[2]["id"] == "G2"
    assert captured_backlog[2]["status"] == "pending"


# ---------------------------------------------------------------------------
# Part B — tiebreak no_diff_mode accept verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tiebreak_accept_overridden_when_no_referenced_files_exist(tmp_path):
    """Bug 22 regression: architect tiebreak in no_diff_mode returns
    ``accept`` but the work item names files that do NOT exist on disk →
    we must override to ``revise_backlog``. The architect cannot honestly
    know the work is done if the target files are missing."""
    from agent.lifecycle.trio.dispatcher import architect_tiebreak

    fake_result = MagicMock(output="prose", tool_calls=[])
    fake_loop = MagicMock()
    fake_loop.run = AsyncMock(return_value=fake_result)
    fake_loop.tool_call_log = []
    extracted = {"action": "accept", "reason": "work already done"}

    work_item = {
        # No ``id`` — reproduces the original bug shape.
        "title": "Create funnel repositories",
        "description": (
            "Create src/harpoon/funnel/repositories.py with FunnelEventRepo "
            "and FunnelExperimentRepo. Both must use src/harpoon/funnel/models.py."
        ),
    }

    # tmp_path has NO src/harpoon/funnel/* — every referenced file is missing.
    with (
        patch(
            "agent.lifecycle.trio.architect.create_architect_agent",
            return_value=fake_loop,
        ),
        patch(
            "agent.lifecycle.trio.extract.extract_tiebreak_decision",
            AsyncMock(return_value=extracted),
        ),
    ):
        decision = await architect_tiebreak(
            parent_task_id=25,
            work_item=work_item,
            transcript=[],
            workspace=str(tmp_path),
            repo_name=None,
            home_dir=None,
            org_id=1,
            no_diff_mode=True,
        )

    assert decision["action"] == "revise_backlog", (
        f"Expected accept→revise_backlog override; got {decision}"
    )
    # The override should leave a breadcrumb for the next architect round.
    reason = (decision.get("reason") or "").lower()
    assert "missing" in reason or "do not exist" in reason or "no such" in reason


@pytest.mark.asyncio
async def test_tiebreak_accept_preserved_when_referenced_files_exist(tmp_path):
    """If the architect says ``accept`` AND at least one referenced file
    exists on disk, the verdict stands. We only override on the
    pathological case where none of the named files exist."""
    from agent.lifecycle.trio.dispatcher import architect_tiebreak

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "existing.py").write_text("# real file\n")

    fake_result = MagicMock(output="prose", tool_calls=[])
    fake_loop = MagicMock()
    fake_loop.run = AsyncMock(return_value=fake_result)
    fake_loop.tool_call_log = []
    extracted = {"action": "accept", "reason": "rename already in place"}

    work_item = {
        "id": "T1",
        "title": "Rename method in src/existing.py",
        "description": "Rename the method in src/existing.py to the new name.",
    }

    with (
        patch(
            "agent.lifecycle.trio.architect.create_architect_agent",
            return_value=fake_loop,
        ),
        patch(
            "agent.lifecycle.trio.extract.extract_tiebreak_decision",
            AsyncMock(return_value=extracted),
        ),
    ):
        decision = await architect_tiebreak(
            parent_task_id=25,
            work_item=work_item,
            transcript=[],
            workspace=str(tmp_path),
            repo_name=None,
            home_dir=None,
            org_id=1,
            no_diff_mode=True,
        )

    assert decision["action"] == "accept"
    assert decision["reason"] == "rename already in place"


@pytest.mark.asyncio
async def test_tiebreak_accept_preserved_when_description_has_no_file_paths(tmp_path):
    """Skip the verification entirely when the work item description
    doesn't mention any file paths — we have nothing to verify against."""
    from agent.lifecycle.trio.dispatcher import architect_tiebreak

    fake_result = MagicMock(output="prose", tool_calls=[])
    fake_loop = MagicMock()
    fake_loop.run = AsyncMock(return_value=fake_result)
    fake_loop.tool_call_log = []
    extracted = {"action": "accept", "reason": "intent reflected"}

    work_item = {
        "id": "T2",
        "title": "Improve naming convention consistency",
        "description": "Make sure variable names follow the project convention.",
    }

    with (
        patch(
            "agent.lifecycle.trio.architect.create_architect_agent",
            return_value=fake_loop,
        ),
        patch(
            "agent.lifecycle.trio.extract.extract_tiebreak_decision",
            AsyncMock(return_value=extracted),
        ),
    ):
        decision = await architect_tiebreak(
            parent_task_id=25,
            work_item=work_item,
            transcript=[],
            workspace=str(tmp_path),
            repo_name=None,
            home_dir=None,
            org_id=1,
            no_diff_mode=True,
        )

    assert decision["action"] == "accept"


@pytest.mark.asyncio
async def test_backfill_backlog_ids_stamps_missing_ids_in_place():
    """Defensive heal: ``_backfill_backlog_ids`` rewrites a parent's
    ``trio_backlog`` so every item has an ``id``. Idempotent — re-running
    on an already-healthy backlog returns ``False`` (no changes).

    Why this exists: ``_mark_item_done`` matches items by ``id``, so a
    legacy backlog with IDless items (the harpoon #25 shape — gap-fix
    architect emitted 13 items pre-fix) loops forever on the first
    pending IDless item: tiebreak ``accept`` calls ``_mark_item_done``
    with ``item_id="(unknown)"``, no item matches, the dispatcher loops.
    """
    from agent.lifecycle.trio import _backfill_backlog_ids

    captured: dict = {}

    class FakeTask:
        def __init__(self) -> None:
            self.trio_backlog: list[dict] = [
                {"id": "T1", "title": "x", "status": "done"},
                {"title": "no id", "status": "pending"},
                {"id": "", "title": "blank id", "status": "pending"},
                {"id": "T2", "title": "y", "status": "pending"},
            ]

    fake_task = FakeTask()

    class FakeResult:
        def scalar_one(self):
            return fake_task

    class FakeSession:
        async def execute(self, *_a, **_k):
            return FakeResult()

        async def commit(self):
            captured["backlog"] = list(fake_task.trio_backlog)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    from agent.lifecycle import trio as trio_mod

    with patch.object(trio_mod, "async_session", lambda: FakeSession()):
        changed = await _backfill_backlog_ids(42)

    assert changed is True
    out = captured["backlog"]
    # T1 + auto-G1 + auto-G2 + T2 — explicit IDs preserved, blanks filled.
    assert [b["id"] for b in out] == ["T1", "G1", "G2", "T2"]


@pytest.mark.asyncio
async def test_backfill_backlog_ids_noop_when_all_ids_present():
    """When every item already has an id, the function returns False and
    does NOT commit (no JSONB write churn / no spurious history row)."""
    from agent.lifecycle.trio import _backfill_backlog_ids

    committed: list[bool] = []

    class FakeTask:
        def __init__(self) -> None:
            self.trio_backlog: list[dict] = [
                {"id": "T1", "title": "x", "status": "done"},
                {"id": "T2", "title": "y", "status": "pending"},
            ]

    class FakeResult:
        def scalar_one(self):
            return FakeTask()

    class FakeSession:
        async def execute(self, *_a, **_k):
            return FakeResult()

        async def commit(self):
            committed.append(True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    from agent.lifecycle import trio as trio_mod

    with patch.object(trio_mod, "async_session", lambda: FakeSession()):
        changed = await _backfill_backlog_ids(42)

    assert changed is False
    assert committed == []


@pytest.mark.asyncio
async def test_tiebreak_verification_skipped_outside_no_diff_mode(tmp_path):
    """The verification ONLY runs in ``no_diff_mode``. For ordinary
    coder↔reviewer ties the architect has the full transcript and the
    file-existence heuristic isn't appropriate."""
    from agent.lifecycle.trio.dispatcher import architect_tiebreak

    fake_result = MagicMock(output="prose", tool_calls=[])
    fake_loop = MagicMock()
    fake_loop.run = AsyncMock(return_value=fake_result)
    fake_loop.tool_call_log = []
    extracted = {"action": "accept", "reason": "coder right"}

    work_item = {
        "id": "T3",
        "title": "Build src/totally/missing.py",
        "description": "Create src/totally/missing.py with the new helper.",
    }

    with (
        patch(
            "agent.lifecycle.trio.architect.create_architect_agent",
            return_value=fake_loop,
        ),
        patch(
            "agent.lifecycle.trio.extract.extract_tiebreak_decision",
            AsyncMock(return_value=extracted),
        ),
    ):
        decision = await architect_tiebreak(
            parent_task_id=25,
            work_item=work_item,
            transcript=[],
            workspace=str(tmp_path),
            repo_name=None,
            home_dir=None,
            org_id=1,
            no_diff_mode=False,  # ordinary tie — verification must NOT fire
        )

    assert decision["action"] == "accept"
