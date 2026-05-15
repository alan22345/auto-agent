"""Tree-sitter-driven code graph analyser (ADR-016 §10, Phase 2).

Public entry points:

* :func:`run_pipeline` — full analysis of a workspace directory; returns a
  :class:`shared.types.RepoGraphBlob`.
* :func:`analyser_version` — single source of truth for the analyser
  version string written into ``RepoGraph.analyser_version`` and the
  blob. Bump this when changing parser behaviour so downstream consumers
  can tell graphs apart.

The package is split by concern:

* ``parsers/`` — one file per language. ``parsers/__init__.py`` holds the
  abstract :class:`Parser` base + the file-extension registry. Phase 2
  ships ``parsers/python.py`` only; adding TypeScript in Phase 4 is a
  single new file plus one registration line.
* ``pipeline.py`` — area discovery, per-area parser dispatch, failure
  isolation, blob assembly.
"""

from __future__ import annotations

from agent.graph_analyzer.pipeline import analyser_version, run_pipeline

__all__ = ["analyser_version", "run_pipeline"]
