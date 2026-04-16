"""Tool result cache — avoids redundant glob/grep calls within a session.

Caches results by tool name + arguments hash. Cache entries are invalidated
when write operations occur on files matching the cached query.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from agent.tools.base import ToolResult


@dataclass
class CacheEntry:
    """A cached tool result."""
    result: ToolResult
    timestamp: float
    tool_name: str


class ToolCache:
    """Simple in-memory cache for read-only tool results.

    Invalidated when write operations happen. Keyed on
    (tool_name, arguments) hash.
    """

    # Tools whose results can be cached
    CACHEABLE_TOOLS = frozenset({"glob", "grep"})

    def __init__(self, max_entries: int = 64) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._max_entries = max_entries
        self._generation = 0  # Bumped on every write operation

    def get(self, tool_name: str, arguments: dict) -> ToolResult | None:
        """Look up a cached result. Returns None on miss."""
        if tool_name not in self.CACHEABLE_TOOLS:
            return None

        key = self._cache_key(tool_name, arguments)
        entry = self._entries.get(key)
        if entry is None:
            return None

        return entry.result

    def put(self, tool_name: str, arguments: dict, result: ToolResult) -> None:
        """Store a tool result in the cache."""
        if tool_name not in self.CACHEABLE_TOOLS:
            return
        if result.is_error:
            return  # Don't cache errors

        key = self._cache_key(tool_name, arguments)

        # Evict oldest if at capacity
        if len(self._entries) >= self._max_entries and key not in self._entries:
            oldest_key = min(self._entries, key=lambda k: self._entries[k].timestamp)
            del self._entries[oldest_key]

        self._entries[key] = CacheEntry(
            result=result,
            timestamp=time.monotonic(),
            tool_name=tool_name,
        )

    def invalidate_on_write(self, tool_name: str) -> None:
        """Invalidate cache entries when a write operation occurs.

        For simplicity, we clear all cached entries on any write,
        since file writes can affect glob/grep results unpredictably.
        """
        if tool_name in ("file_write", "file_edit", "bash"):
            self._entries.clear()
            self._generation += 1

    @property
    def size(self) -> int:
        return len(self._entries)

    @staticmethod
    def _cache_key(tool_name: str, arguments: dict) -> str:
        """Generate a stable cache key from tool name + arguments."""
        args_str = json.dumps(arguments, sort_keys=True, default=str)
        return hashlib.md5(f"{tool_name}:{args_str}".encode()).hexdigest()
