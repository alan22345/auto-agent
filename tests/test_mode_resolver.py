"""ADR-015 §7 — effective-mode resolution (per-repo default + per-task override).

The mode resolver is a single primitive called at every gate: it answers
"is this task being driven by a human or by a standin?" via the rule

    effective_mode = task.mode_override or repo.mode

with both fields strictly in ``{"freeform", "human_in_loop"}``. The
override is bidirectional — a freeform repo can force human review on
one task, and a human-in-loop repo can flip a single task to freeform.

These tests pin the resolver's contract without needing the DB: they
construct lightweight stand-ins via ``types.SimpleNamespace`` so the
test stays pure-function and runs in-memory.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.lifecycle.mode_resolver import resolve_effective_mode


def _task(mode_override: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(mode_override=mode_override)


def _repo(mode: str = "human_in_loop") -> SimpleNamespace:
    return SimpleNamespace(mode=mode)


def test_no_override_inherits_repo_freeform() -> None:
    assert resolve_effective_mode(_task(None), _repo("freeform")) == "freeform"


def test_no_override_inherits_repo_human_in_loop() -> None:
    assert resolve_effective_mode(_task(None), _repo("human_in_loop")) == "human_in_loop"


def test_override_human_in_loop_on_freeform_repo() -> None:
    """Override-down — a freeform repo asks for human review on this task."""

    assert resolve_effective_mode(_task("human_in_loop"), _repo("freeform")) == "human_in_loop"


def test_override_freeform_on_human_in_loop_repo() -> None:
    """Override-up — a normally-human repo runs this one task freeform."""

    assert resolve_effective_mode(_task("freeform"), _repo("human_in_loop")) == "freeform"


def test_unknown_repo_mode_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_effective_mode(_task(None), _repo("yolo"))


def test_unknown_override_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_effective_mode(_task("yolo"), _repo("human_in_loop"))


def test_missing_repo_mode_defaults_to_human_in_loop() -> None:
    """If ``repo.mode`` is missing (e.g. legacy row before migration ran),
    default to the safe choice — keep the human in the loop."""

    repo = SimpleNamespace()  # no .mode attr
    assert resolve_effective_mode(_task(None), repo) == "human_in_loop"
