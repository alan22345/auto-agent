"""Filter orchestrator-scope env vars out of a child-process env.

ADR-019 §6: scaffolded projects must not inherit auto-agent's operational
credentials. ``filtered_host_env`` returns ``os.environ`` minus everything
``shared.config.reserved_env_keys()`` declares as orchestrator-owned.
"""

from __future__ import annotations

import os

from shared.config import reserved_env_keys


def filtered_host_env() -> dict[str, str]:
    """Return ``os.environ`` minus orchestrator-scope keys.

    Case-insensitive key comparison — ``ANTHROPIC_API_KEY`` and
    ``anthropic_api_key`` both get filtered.
    """
    reserved = reserved_env_keys()
    return {k: v for k, v in os.environ.items() if k.upper() not in reserved}
