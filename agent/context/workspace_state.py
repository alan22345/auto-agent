"""Workspace state tracker — tracks files read, modified, and tested during a session.

Prevents redundant re-reads of unchanged files and provides the agent with
a running summary of what it has done so far.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FileAction(Enum):
    READ = "read"
    WRITTEN = "written"
    EDITED = "edited"


@dataclass
class FileState:
    """Tracked state for a single file."""
    path: str
    actions: list[FileAction] = field(default_factory=list)
    read_count: int = 0
    last_read_turn: int = -1
    modified_turn: int = -1  # Turn when file was last written/edited

    @property
    def was_modified(self) -> bool:
        return self.modified_turn >= 0

    @property
    def is_stale_read(self) -> bool:
        """True if file was read BEFORE it was last modified (needs re-read)."""
        return self.was_modified and self.last_read_turn < self.modified_turn


@dataclass
class WorkspaceState:
    """Tracks what the agent has done during a session."""

    files: dict[str, FileState] = field(default_factory=dict)
    bash_commands: list[str] = field(default_factory=list)
    test_runs: list[str] = field(default_factory=list)
    current_turn: int = 0

    def record_read(self, path: str) -> str | None:
        """Record a file read. Returns a warning if redundant, else None."""
        state = self.files.setdefault(path, FileState(path=path))
        state.read_count += 1
        state.actions.append(FileAction.READ)
        state.last_read_turn = self.current_turn

        # Warn if reading the same unmodified file 3+ times
        if state.read_count >= 3 and not state.is_stale_read:
            return f"Note: You have read {path} {state.read_count} times without modifying it."
        return None

    def record_write(self, path: str) -> None:
        """Record a file write/create."""
        state = self.files.setdefault(path, FileState(path=path))
        state.actions.append(FileAction.WRITTEN)
        state.modified_turn = self.current_turn

    def record_edit(self, path: str) -> None:
        """Record a file edit."""
        state = self.files.setdefault(path, FileState(path=path))
        state.actions.append(FileAction.EDITED)
        state.modified_turn = self.current_turn

    def record_bash(self, command: str) -> None:
        """Record a bash command execution."""
        self.bash_commands.append(command)

    def record_test_run(self, command: str) -> None:
        """Record that tests were run."""
        self.test_runs.append(command)

    def advance_turn(self) -> None:
        """Increment the turn counter."""
        self.current_turn += 1

    def summary(self) -> str:
        """Produce a compact summary of workspace activity for context injection."""
        if not self.files and not self.bash_commands and not self.test_runs:
            return ""

        parts: list[str] = []

        modified = [p for p, s in self.files.items() if s.was_modified]
        read_only = [p for p, s in self.files.items() if not s.was_modified and s.read_count > 0]

        if modified:
            parts.append(f"Files modified: {', '.join(sorted(modified))}")
        if read_only:
            parts.append(f"Files read: {', '.join(sorted(read_only[:10]))}")
            if len(read_only) > 10:
                parts.append(f"  ... and {len(read_only) - 10} more")
        if self.test_runs:
            parts.append(f"Tests run: {len(self.test_runs)} time(s)")

        return "\n".join(parts)

    def process_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """Process a tool call and update state. Returns optional warning message."""
        warning = None

        if tool_name == "file_read":
            path = arguments.get("file_path", "")
            warning = self.record_read(path)

        elif tool_name == "file_write":
            path = arguments.get("file_path", "")
            self.record_write(path)

        elif tool_name == "file_edit":
            path = arguments.get("file_path", "")
            self.record_edit(path)

        elif tool_name == "bash":
            cmd = arguments.get("command", "")
            self.record_bash(cmd)

        return warning
