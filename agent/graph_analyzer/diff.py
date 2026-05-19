"""Compute a ChangedFilesPlan from the workspace's git diff.

Used by ``agent/lifecycle/graph_refresh.py`` to decide which files to
re-walk when the checkpoint commit no longer matches HEAD.

The plan distinguishes pure renames (similarity = 100%) from rename+modify
because pure renames let us rewrite paths on existing nodes/edges without
re-walking the file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class ChangedFilesPlan:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)  # M and T merge here
    deleted: list[str] = field(default_factory=list)
    renamed_pure: list[tuple[str, str]] = field(default_factory=list)
    renamed_modified: list[tuple[str, str]] = field(default_factory=list)


def parse_git_name_status(raw: bytes) -> ChangedFilesPlan:
    """Parse NUL-separated output of:

        git diff --name-status --diff-filter=AMRTUD -z <from> <to>

    The output alternates status token + path token(s), each NUL-terminated.
    """
    plan = ChangedFilesPlan()
    tokens = raw.split(b"\x00")
    if tokens and tokens[-1] == b"":
        tokens = tokens[:-1]

    i = 0
    while i < len(tokens):
        status = tokens[i].decode("utf-8")
        if status.startswith("R"):
            old_path = tokens[i + 1].decode("utf-8")
            new_path = tokens[i + 2].decode("utf-8")
            try:
                similarity = int(status[1:])
            except ValueError:
                similarity = 0
            if similarity >= 100:
                plan.renamed_pure.append((old_path, new_path))
            else:
                plan.renamed_modified.append((old_path, new_path))
            i += 3
            continue

        path = tokens[i + 1].decode("utf-8")
        if status == "A":
            plan.added.append(path)
        elif status in ("M", "T"):
            plan.modified.append(path)
        elif status == "D":
            plan.deleted.append(path)
        i += 2

    return plan


class CheckpointCommitUnreachable(Exception):
    """Raised when `git diff <from> <to>` errors because <from> is no longer
    reachable. Caller should fall back to full re-analysis from scratch."""


async def changed_files(
    workspace: str, from_sha: str, to_sha: str
) -> ChangedFilesPlan:
    """Run `git diff --name-status -z` and parse the result.

    Raises CheckpointCommitUnreachable if from_sha doesn't exist in the
    repository (typical after a force-push)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--name-status",
        "--diff-filter=AMRTUD",
        "-z",
        from_sha,
        to_sha,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        if "unknown revision" in err or "bad revision" in err or "fatal" in err:
            raise CheckpointCommitUnreachable(err.strip())
        raise RuntimeError(f"git diff failed: {err.strip()}")
    return parse_git_name_status(stdout)
