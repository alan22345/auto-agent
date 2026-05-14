"""Read primitives for skill-emitted workspace files — ADR-015 §12.

Every gated agent action writes a JSON or markdown file under the
``.auto-agent/`` workspace directory; the orchestrator reads it after
``agent.run`` returns. This module owns the read side of that contract.

Three primitives:

- :func:`read_gate_file` — returns parsed JSON (``dict``), markdown text
  (``str``), or ``None`` if the file is missing. JSON payloads carrying
  a ``schema_version`` field are validated against the caller's expected
  version; a mismatch raises :class:`ValueError`.
- :func:`gate_file_exists` — boolean existence check.
- :func:`expect_gate_file` — like :func:`read_gate_file` but raises
  :class:`MissingGateFileError` instead of returning ``None``. Used by
  the orchestrator's retry-then-escalate path in later phases.

The module intentionally does not own state-machine wiring or retry
logic; those live in the orchestrator. This is plumbing only.
"""

from __future__ import annotations

import json
import os


class MissingGateFileError(FileNotFoundError):
    """Raised by :func:`expect_gate_file` when the file is absent.

    Inherits :class:`FileNotFoundError` so callers that already trap
    ``OSError`` family exceptions continue to work; the dedicated class
    lets the orchestrator distinguish "skill didn't run" from generic
    filesystem errors.
    """


def _absolute_path(workspace_root: str, relative_path: str) -> str:
    """Join a workspace-relative path with its root."""

    return os.path.join(workspace_root, relative_path)


def read_gate_file(
    workspace_root: str,
    relative_path: str,
    schema_version: str = "1",
) -> dict | str | None:
    """Read a skill-emitted gate file.

    Args:
        workspace_root: Absolute path to the workspace root.
        relative_path: One of the constants in
            :mod:`agent.lifecycle.workspace_paths`.
        schema_version: Expected ``schema_version`` for JSON payloads. The
            check is opt-in — only enforced when the file actually carries
            the field. Markdown files are returned as-is.

    Returns:
        - ``dict`` for ``.json`` files
        - ``str`` for ``.md`` files
        - ``None`` when the file does not exist

    Raises:
        ValueError: When the file is JSON and either malformed or carries
            a ``schema_version`` that disagrees with the caller's
            expectation.
    """

    abs_path = _absolute_path(workspace_root, relative_path)
    if not os.path.isfile(abs_path):
        return None

    if relative_path.endswith(".json"):
        try:
            with open(abs_path) as fh:
                payload = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"gate file {relative_path} is not valid JSON: {exc}") from exc
        if isinstance(payload, dict):
            present = payload.get("schema_version")
            if present is not None and present != schema_version:
                raise ValueError(
                    f"gate file {relative_path} schema_version "
                    f"{present!r} does not match expected {schema_version!r}"
                )
        return payload

    # Markdown (or anything else not .json) is returned verbatim.
    with open(abs_path) as fh:
        return fh.read()


def gate_file_exists(workspace_root: str, relative_path: str) -> bool:
    """Boolean existence check — does not parse the file."""

    return os.path.isfile(_absolute_path(workspace_root, relative_path))


def expect_gate_file(
    workspace_root: str,
    relative_path: str,
    schema_version: str = "1",
) -> dict | str:
    """Read a gate file or raise :class:`MissingGateFileError`.

    The orchestrator's retry-then-escalate path (Phase 4+) uses this to
    distinguish "skill didn't run" from "skill ran and wrote something
    invalid" — the missing case is recoverable by re-invoking the agent
    with an amended prompt, the invalid case escalates.
    """

    result = read_gate_file(workspace_root, relative_path, schema_version)
    if result is None:
        raise MissingGateFileError(f"expected gate file {relative_path} under {workspace_root}")
    return result
