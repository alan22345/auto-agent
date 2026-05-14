"""Phase 11 rename — ``architect_analyzer`` → ``improvement_agent`` (ADR-015 §14).

Pins the load-bearing post-rename invariants:

  * ``agent.improvement_agent`` is the canonical module name; its public
    symbols (``run_architecture_loop``, ``_is_due``,
    ``handle_architecture_analysis``) import cleanly.
  * The old ``agent.architect_analyzer`` import path no longer resolves —
    no compatibility shim, per the ADR's "one-shot rename" stance.
"""

from __future__ import annotations

import importlib


def test_improvement_agent_module_imports() -> None:
    mod = importlib.import_module("agent.improvement_agent")
    assert hasattr(mod, "run_architecture_loop")
    assert hasattr(mod, "_is_due")
    assert hasattr(mod, "handle_architecture_analysis")


def test_old_architect_analyzer_module_is_gone() -> None:
    """The old path must not import — no compatibility shim left behind."""
    try:
        importlib.import_module("agent.architect_analyzer")
    except ModuleNotFoundError:
        return
    raise AssertionError("agent.architect_analyzer still importable — rename is incomplete")
