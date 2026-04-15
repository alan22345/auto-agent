"""Session persistence — save/load conversation state across agent phases.

Maintains two message arrays:
- messages: Full history (persistent, enables re-compaction)
- api_messages: API-ready view (compacted, what the model actually sees)
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog

from agent.llm.types import Message, ToolCall

logger = structlog.get_logger()

# Default storage location (relative to workspace or absolute)
DEFAULT_SESSIONS_DIR = ".sessions"


class Session:
    """JSON file-based session persistence."""

    def __init__(self, session_id: str, storage_dir: str | None = None) -> None:
        self.session_id = session_id
        self._dir = storage_dir or os.path.join(
            os.path.dirname(os.path.dirname(__file__)), DEFAULT_SESSIONS_DIR
        )
        self._path = os.path.join(self._dir, f"{session_id}.json")

    async def load(self) -> tuple[list[Message], list[Message]] | None:
        """Load both message arrays from disk.

        Returns (full_history, api_messages) or None if no session exists.
        """
        if not os.path.isfile(self._path):
            return None

        try:
            with open(self._path, "r") as f:
                data = json.load(f)
            messages = [self._deserialize_message(m) for m in data.get("messages", [])]
            api_messages = [self._deserialize_message(m) for m in data.get("api_messages", [])]
            logger.info("session_loaded", session_id=self.session_id, messages=len(messages))
            return messages, api_messages
        except Exception as e:
            logger.warning("session_load_failed", session_id=self.session_id, error=str(e))
            return None

    async def save(self, messages: list[Message], api_messages: list[Message]) -> None:
        """Save both message arrays to disk."""
        os.makedirs(self._dir, exist_ok=True)

        data = {
            "session_id": self.session_id,
            "messages": [self._serialize_message(m) for m in messages],
            "api_messages": [self._serialize_message(m) for m in api_messages],
        }

        try:
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug("session_saved", session_id=self.session_id, messages=len(messages))
        except Exception as e:
            logger.error("session_save_failed", session_id=self.session_id, error=str(e))

    async def delete(self) -> None:
        """Remove the session file."""
        if os.path.isfile(self._path):
            os.remove(self._path)
            logger.info("session_deleted", session_id=self.session_id)

    @staticmethod
    def _serialize_message(msg: Message) -> dict[str, Any]:
        data: dict[str, Any] = {
            "role": msg.role,
            "content": msg.content,
        }
        if msg.tool_calls:
            data["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            data["tool_call_id"] = msg.tool_call_id
        if msg.tool_name:
            data["tool_name"] = msg.tool_name
        if msg.token_estimate is not None:
            data["token_estimate"] = msg.token_estimate
        return data

    @staticmethod
    def _deserialize_message(data: dict[str, Any]) -> Message:
        tool_calls = None
        if "tool_calls" in data:
            tool_calls = [
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in data["tool_calls"]
            ]
        return Message(
            role=data["role"],
            content=data["content"],
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id"),
            tool_name=data.get("tool_name"),
            token_estimate=data.get("token_estimate"),
        )
