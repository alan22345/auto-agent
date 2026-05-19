"""Compute a ChangedFilesPlan from the workspace's git diff.

Used by ``agent/lifecycle/graph_refresh.py`` to decide which files to
re-walk when the checkpoint commit no longer matches HEAD.

The plan distinguishes pure renames (similarity = 100%) from rename+modify
because pure renames let us rewrite paths on existing nodes/edges without
re-walking the file.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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


def apply_plan(
    blob: dict,
    processed: dict,
    plan: ChangedFilesPlan,
    re_walk: Callable[[str], dict] | None = None,
) -> set[str]:
    """Mutate ``blob`` and ``processed`` per ``plan``. Return the set of
    additional files that need to be re-walked (the cascade set).

    ``re_walk`` is a callback used for the smart-cascade-on-M check: given
    a modified file, return ``{"nodes_in_path": [...]}`` describing the
    fresh-walk node set. apply_plan compares that to the old node ids to
    detect which previously-referenced nodes were lost.

    Files in the cascade set have their ``processed`` entry dropped so the
    pipeline's main walk picks them up again in the same run.
    """
    cascade: set[str] = set()

    # --- D ---
    for path in plan.deleted:
        cross_file_callers = {
            e["source"]["file"]
            for e in blob.get("edges", [])
            if e["target"]["file"] == path and e["source"]["file"] != path
        }
        cascade.update(cross_file_callers)
        blob["nodes"] = [n for n in blob.get("nodes", []) if n["file"] != path]
        blob["edges"] = [
            e
            for e in blob.get("edges", [])
            if e["source"]["file"] != path and e["target"]["file"] != path
        ]
        processed.pop(path, None)
    # Drop processed entries for all cascade files collected during D
    for caller_file in cascade:
        processed.pop(caller_file, None)

    # --- M / T (with smart cascade) ---
    for path in plan.modified:
        cross_file_targets = {
            e["target"]["id"]
            for e in blob.get("edges", [])
            if e["target"]["file"] == path and e["source"]["file"] != path
        }
        callers_by_target: dict[str, set[str]] = {}
        for e in blob.get("edges", []):
            if e["target"]["file"] == path and e["source"]["file"] != path:
                callers_by_target.setdefault(e["target"]["id"], set()).add(
                    e["source"]["file"]
                )

        blob["nodes"] = [n for n in blob.get("nodes", []) if n["file"] != path]
        blob["edges"] = [
            e
            for e in blob.get("edges", [])
            if e["source"]["file"] != path and e["target"]["file"] != path
        ]
        processed.pop(path, None)

        if re_walk is not None and cross_file_targets:
            walk_result = re_walk(path)
            new_ids = {n["id"] for n in walk_result.get("nodes_in_path", [])}
            still_lost = cross_file_targets - new_ids
            for lost_id in still_lost:
                for caller_file in callers_by_target.get(lost_id, set()):
                    cascade.add(caller_file)
                    processed.pop(caller_file, None)

    # --- R100: pure rename, rewrite paths ---
    for old, new in plan.renamed_pure:
        for n in blob.get("nodes", []):
            if n["file"] == old:
                n["file"] = new
                if n.get("id", "").startswith(f"{old}::"):
                    n["id"] = n["id"].replace(f"{old}::", f"{new}::", 1)
        for e in blob.get("edges", []):
            if e["source"]["file"] == old:
                e["source"]["file"] = new
                if e["source"].get("id", "").startswith(f"{old}::"):
                    e["source"]["id"] = e["source"]["id"].replace(
                        f"{old}::", f"{new}::", 1
                    )
            if e["target"]["file"] == old:
                e["target"]["file"] = new
                if e["target"].get("id", "").startswith(f"{old}::"):
                    e["target"]["id"] = e["target"]["id"].replace(
                        f"{old}::", f"{new}::", 1
                    )
        if old in processed:
            processed[new] = processed.pop(old)

    # --- R<low>: rename + modify (treat as D old + A new) ---
    if plan.renamed_modified:
        synth = ChangedFilesPlan(deleted=[o for o, _ in plan.renamed_modified])
        cascade.update(apply_plan(blob, processed, synth, re_walk=None))

    return cascade
