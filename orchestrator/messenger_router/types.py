"""Internal types for the messenger router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime

FocusKind = Literal["draft", "task", "none"]
"""v1 focus kinds. v2 will add 'freeform' and 'po_analysis'."""

# 24h focus TTL, per design.
FOCUS_TTL_HOURS = 24

# Cap conversation history rows at the most recent N messages.
MAX_HISTORY_MESSAGES = 200


@dataclass
class LoadedConversation:
    """In-memory view of a single conversation row."""

    conversation_id: int
    user_id: int
    source: str
    focus_kind: FocusKind
    focus_id: int | None
    messages: list[dict[str, Any]]  # raw message dicts as stored in jsonb
    last_active_at: datetime
