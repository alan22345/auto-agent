"""Deprecated — use run.py as the single entrypoint.

This module previously duplicated the orchestrator event loop and FastAPI app.
All functionality now lives in run.py which starts everything in one process.
"""

raise ImportError(
    "orchestrator.main is deprecated. Use 'python run.py' instead."
)
