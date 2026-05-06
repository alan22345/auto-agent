"""PTY-driven Claude OAuth pairing.

Spawns ``claude setup-token`` inside a pseudo-terminal so the CLI's interactive
login flow works without a real TTY. The login URL is read off stdout and
forwarded to the browser; the user's pasted one-time code is written back
into the PTY's stdin. On success, the CLI writes credentials into the
user's vault directory (``$HOME/.claude/.credentials.json``) and exits 0.

Sessions are kept in an in-process registry keyed by a UUID ``pairing_id``,
with a 5-minute TTL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import select
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import ptyprocess

from orchestrator.claude_auth import ensure_vault_dir
from shared.config import settings

log = logging.getLogger(__name__)

PAIRING_TTL_SECONDS = 300
PAIRING_COMMAND = ["claude", "setup-token"]


@dataclass
class PairingResult:
    success: bool
    stderr: str
    exit_code: int


class PairingSession:
    def __init__(self, user_id: int, home_dir: str):
        self.pairing_id = str(uuid.uuid4())
        self.user_id = user_id
        self.home_dir = home_dir
        self.created_at = time.time()
        env = {**os.environ, "HOME": home_dir}
        self._proc = ptyprocess.PtyProcess.spawn(
            PAIRING_COMMAND, env=env, cwd=home_dir
        )
        self._buffer = ""
        self._closed = False

    def _drain_available(self) -> None:
        """Pull any currently-readable bytes from the PTY into the buffer.

        Uses select() so we never block — important because asyncio executors
        have a limited thread pool and a blocking ptyprocess.read() would pin
        a thread until either data arrives or the process exits.
        """
        fd = self._proc.fd
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0)
            except OSError:
                return
            if not r:
                return
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                self._closed = True
                return
            if not chunk:
                self._closed = True
                return
            self._buffer += chunk.decode("utf-8", errors="replace")

    async def read_line(self, timeout: float = 1.0) -> Optional[str]:
        """Read one line from the PTY, with timeout. Returns None on timeout."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            self._drain_available()
            if "\n" in self._buffer:
                line, _, rest = self._buffer.partition("\n")
                self._buffer = rest
                return line + "\n"
            if self._closed and self._buffer:
                out, self._buffer = self._buffer, ""
                return out
            if self._closed or loop.time() >= deadline:
                return None
            await asyncio.sleep(0.05)

    async def submit_code(self, code: str) -> None:
        """Write the user's pasted one-time code into the PTY's stdin."""
        loop = asyncio.get_event_loop()
        payload = (code + "\n").encode("utf-8")
        await loop.run_in_executor(None, self._proc.write, payload)

    async def wait_for_exit(self, timeout: float = 30.0) -> PairingResult:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            # Drain output continuously — otherwise the PTY buffer can fill and
            # block the child, masquerading as a "process never exits" hang.
            self._drain_available()
            if not self._proc.isalive():
                break
            await asyncio.sleep(0.1)
        if self._proc.isalive():
            self._proc.kill(9)
            _registry.pop(self.pairing_id, None)
            return PairingResult(False, "pairing timed out", -1)

        # Final drain after exit.
        self._drain_available()

        exit_code = self._proc.exitstatus or 0
        cred_path = os.path.join(self.home_dir, ".claude", ".credentials.json")
        success = exit_code == 0 and os.path.exists(cred_path)
        _registry.pop(self.pairing_id, None)
        return PairingResult(
            success=success,
            stderr="" if success else self._buffer[-500:],
            exit_code=exit_code,
        )

    async def cancel(self) -> None:
        if self._proc.isalive():
            self._proc.kill(9)
        _registry.pop(self.pairing_id, None)


_registry: dict[str, PairingSession] = {}


async def start_pairing(user_id: int) -> PairingSession:
    home_dir = ensure_vault_dir(user_id)
    _gc_expired()
    session = PairingSession(user_id, home_dir)
    _registry[session.pairing_id] = session
    return session


def get_pairing(pairing_id: str) -> Optional[PairingSession]:
    _gc_expired()
    return _registry.get(pairing_id)


def _gc_expired() -> None:
    now = time.time()
    for pid, sess in list(_registry.items()):
        if now - sess.created_at > PAIRING_TTL_SECONDS:
            try:
                if sess._proc.isalive():
                    sess._proc.kill(9)
            except Exception:
                pass
            _registry.pop(pid, None)
