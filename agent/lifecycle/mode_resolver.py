"""Effective-mode resolver — ADR-015 §7.

A single pure function used by every gate to answer "is this task driven
by a human or by a standin agent right now?"

The rule, ``effective_mode = task.mode_override or repo.mode``, encodes
two design choices the ADR locks in:

1. **Per-repo default**: every repo carries a ``mode`` flag —
   ``"freeform"`` or ``"human_in_loop"``. This is the default for every
   task created against the repo.
2. **Bidirectional per-task override**: ``Task.mode_override`` can flip
   either way. A freeform repo can force human review on one
   sensitive task; a normally-human repo can run a single task
   freeform without flipping the whole repo.

There is no asymmetry — neither side is "stronger" than the other; the
override wins outright. This is deliberate; see ADR-015 §7.
"""

from __future__ import annotations

from typing import Literal

EffectiveMode = Literal["freeform", "human_in_loop"]

_VALID_MODES: frozenset[str] = frozenset({"freeform", "human_in_loop"})


def resolve_effective_mode(task, repo) -> EffectiveMode:
    """Return ``"freeform"`` or ``"human_in_loop"`` for ``task`` against ``repo``.

    Args:
        task: Anything with ``mode_override: str | None``. The ORM ``Task``
            row works; tests pass ``types.SimpleNamespace`` shims.
        repo: Anything with ``mode: str``. Missing attribute is treated
            as the conservative default ``"human_in_loop"`` so legacy
            rows from before migration 039 ran route safely.

    Raises:
        ValueError: ``mode_override`` or ``repo.mode`` is non-null and
            outside the two-element domain.
    """

    override = getattr(task, "mode_override", None)
    if override is not None:
        if override not in _VALID_MODES:
            raise ValueError(
                f"task.mode_override must be one of {sorted(_VALID_MODES)}; got {override!r}"
            )
        return override  # type: ignore[return-value]

    repo_mode = getattr(repo, "mode", None) or "human_in_loop"
    if repo_mode not in _VALID_MODES:
        raise ValueError(f"repo.mode must be one of {sorted(_VALID_MODES)}; got {repo_mode!r}")
    return repo_mode  # type: ignore[return-value]


__all__ = ["EffectiveMode", "resolve_effective_mode"]
