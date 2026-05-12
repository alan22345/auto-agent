"""Dev-server lifecycle utilities used by coding / verify / review phases.

Layers in this module:
- Pure helpers: ``sniff_run_command``.
- Async lifecycle: ``start_dev_server``, ``wait_for_port``, ``hold``, ``kill_server``.
- Agent-callable tool: ``TailDevServerLogTool``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import signal
import socket
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from agent.tools.base import Tool, ToolContext, ToolResult


# ---------------------------------------------------------------------------
# Task 6: sniff_run_command
# ---------------------------------------------------------------------------


def sniff_run_command(workspace_path: str, *, override: str | None = None) -> str | None:
    """Return a shell command that starts the project's dev server.

    Priority: ``override`` (FreeformConfig.run_command) → package.json scripts.dev
    → Procfile ``web:`` entry → pyproject.toml ``[tool.auto-agent].run``.
    Returns ``None`` if nothing resolves.
    """
    if override:
        return override

    workspace = Path(workspace_path)

    pkg = workspace / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text())
            if isinstance(data.get("scripts"), dict) and "dev" in data["scripts"]:
                return "npm run dev"
        except Exception:
            pass

    procfile = workspace / "Procfile"
    if procfile.is_file():
        for line in procfile.read_text().splitlines():
            if line.startswith("web:"):
                return line[len("web:"):].strip()

    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text())
            cmd = data.get("tool", {}).get("auto-agent", {}).get("run")
            if isinstance(cmd, str) and cmd:
                return cmd
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Task 7: DevServerHandle, start_dev_server, kill_server
# ---------------------------------------------------------------------------


@dataclass
class DevServerHandle:
    pid: int
    pgid: int
    port: int
    log_path: str
    started_at: float
    process: asyncio.subprocess.Process = field(repr=False)


def _allocate_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class BootError(RuntimeError):
    """Raised when start_dev_server can't even start (no run command, fork failure)."""


class BootTimeout(RuntimeError):
    def __init__(self, log_tail: str):
        super().__init__("dev server failed to bind port in time")
        self.log_tail = log_tail


class EarlyExit(RuntimeError):
    def __init__(self, log_tail: str):
        super().__init__("dev server exited during hold")
        self.log_tail = log_tail


@contextlib.asynccontextmanager
async def start_dev_server(
    workspace_path: str, *, override: str | None = None,
) -> AsyncIterator[DevServerHandle]:
    cmd = sniff_run_command(workspace_path, override=override)
    if not cmd:
        raise BootError("no run command resolved for workspace")

    port = _allocate_port()
    log_file = tempfile.NamedTemporaryFile(
        prefix="dev-server-", suffix=".log", delete=False, mode="w",
    )
    log_path = log_file.name
    log_file.close()
    log_fh = open(log_path, "wb", buffering=0)

    env = os.environ.copy()
    env["PORT"] = str(port)

    process = await asyncio.create_subprocess_shell(
        cmd,
        cwd=workspace_path,
        env=env,
        stdout=log_fh,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=os.setsid,
    )

    handle = DevServerHandle(
        pid=process.pid,
        pgid=os.getpgid(process.pid),
        port=port,
        log_path=log_path,
        started_at=time.time(),
        process=process,
    )

    try:
        yield handle
    finally:
        await kill_server(handle)
        log_fh.close()


async def kill_server(handle: DevServerHandle, grace_seconds: float = 2.0) -> None:
    try:
        os.killpg(handle.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(handle.process.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(handle.pgid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(handle.process.wait(), timeout=1.0)


# ---------------------------------------------------------------------------
# Task 8: wait_for_port and hold
# ---------------------------------------------------------------------------


def _tail(path: str, lines: int = 50) -> str:
    try:
        data = Path(path).read_text(errors="replace")
    except Exception:
        return ""
    return "\n".join(data.splitlines()[-lines:])


async def wait_for_port(port: int, timeout: float = 60.0, log_path: str | None = None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.25)
            s.connect(("127.0.0.1", port))
            s.close()
            return
        except OSError:
            await asyncio.sleep(0.25)
    raise BootTimeout(_tail(log_path) if log_path else "")


async def hold(handle: DevServerHandle, seconds: float = 5.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if handle.process.returncode is not None:
            raise EarlyExit(_tail(handle.log_path))
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Task 9: TailDevServerLogTool
# ---------------------------------------------------------------------------


class TailDevServerLogTool(Tool):
    name = "tail_dev_server_log"
    description = (
        "Return the last N lines of the dev server log for this task. "
        "Useful when verify or review fails and you need to see what the server printed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "lines": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
        },
        "required": [],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        log_path = getattr(context, "dev_server_log_path", None)
        if not log_path:
            return ToolResult(output="(no dev server running)")
        n = int(arguments.get("lines", 50))
        return ToolResult(output=_tail(log_path, lines=n))
