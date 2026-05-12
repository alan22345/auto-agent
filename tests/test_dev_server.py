"""Tests for agent/tools/dev_server.py — Tasks 6-9."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
from pathlib import Path

import pytest

from agent.tools.base import ToolContext
from agent.tools.dev_server import (
    BootTimeout,
    DevServerHandle,
    EarlyExit,
    TailDevServerLogTool,
    hold,
    kill_server,
    sniff_run_command,
    start_dev_server,
    wait_for_port,
)


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


# ---------------------------------------------------------------------------
# Task 6: sniff_run_command
# ---------------------------------------------------------------------------


def test_sniff_package_json_dev():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "package.json", json.dumps({"scripts": {"dev": "next dev"}}))
        assert sniff_run_command(d) == "npm run dev"


def test_sniff_procfile_web():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "Procfile", "web: python run.py\nworker: rq worker\n")
        assert sniff_run_command(d) == "python run.py"


def test_sniff_pyproject_run():
    with tempfile.TemporaryDirectory() as d:
        _write(
            Path(d) / "pyproject.toml",
            '[tool.auto-agent]\nrun = "uvicorn app:app --reload"\n',
        )
        assert sniff_run_command(d) == "uvicorn app:app --reload"


def test_sniff_priority_freeform_config_wins():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "package.json", json.dumps({"scripts": {"dev": "next dev"}}))
        assert sniff_run_command(d, override="make serve") == "make serve"


def test_sniff_none_when_nothing_resolves():
    with tempfile.TemporaryDirectory() as d:
        assert sniff_run_command(d) is None


# ---------------------------------------------------------------------------
# Task 7: start_dev_server, DevServerHandle, kill_server
# ---------------------------------------------------------------------------


async def test_start_and_kill_simple_server():
    """Spawn a tiny python http.server, confirm port is reachable, then kill it."""
    with tempfile.TemporaryDirectory() as d:
        # Write server script to a file to avoid quoting/newline issues in Procfile.
        srv = Path(d, "srv.py")
        srv.write_text(
            "import http.server, socketserver, os\n"
            "port = int(os.environ['PORT'])\n"
            "with socketserver.TCPServer(('127.0.0.1', port), http.server.SimpleHTTPRequestHandler) as s:\n"
            "    s.serve_forever()\n"
        )
        Path(d, "Procfile").write_text("web: python3 srv.py\n")
        async with start_dev_server(d) as handle:
            assert isinstance(handle, DevServerHandle)
            assert handle.port > 0
            await asyncio.sleep(0.5)
            with socket.create_connection(("127.0.0.1", handle.port), timeout=2):
                pass
        # After context exit, port should refuse.
        with pytest.raises(OSError), socket.create_connection(("127.0.0.1", handle.port), timeout=1):
            pass


# ---------------------------------------------------------------------------
# Task 8: wait_for_port and hold
# ---------------------------------------------------------------------------


def _take_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def test_wait_for_port_success():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        await wait_for_port(port, timeout=1.0)
    finally:
        s.close()


async def test_wait_for_port_timeout():
    port = _take_port()
    with pytest.raises(BootTimeout):
        await wait_for_port(port, timeout=0.2)


async def test_hold_passes_when_alive():
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c", "import time; time.sleep(5)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    handle = DevServerHandle(
        pid=proc.pid,
        pgid=os.getpgid(proc.pid),
        port=0,
        log_path="/dev/null",
        started_at=time.time(),
        process=proc,
    )
    try:
        await hold(handle, seconds=0.5)
    finally:
        await kill_server(handle)


async def test_hold_raises_on_early_exit(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("boom\nbang\n")
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c", "import sys; sys.exit(1)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    handle = DevServerHandle(
        pid=proc.pid,
        pgid=os.getpgid(proc.pid),
        port=0,
        log_path=str(log),
        started_at=time.time(),
        process=proc,
    )
    await asyncio.sleep(0.2)  # let it exit
    with pytest.raises(EarlyExit) as ei:
        await hold(handle, seconds=0.5)
    assert "boom" in ei.value.log_tail


# ---------------------------------------------------------------------------
# Task 9: TailDevServerLogTool
# ---------------------------------------------------------------------------


async def test_tail_log_tool_returns_last_lines(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")

    tool = TailDevServerLogTool()
    ctx = ToolContext(workspace=str(tmp_path))
    ctx.dev_server_log_path = str(log)  # type: ignore[attr-defined]
    result = await tool.execute({"lines": 5}, context=ctx)
    text = result.output
    assert "line 95" in text and "line 99" in text
    assert "line 0" not in text


async def test_tail_log_tool_no_server(tmp_path):
    tool = TailDevServerLogTool()
    ctx = ToolContext(workspace=str(tmp_path))
    ctx.dev_server_log_path = None  # type: ignore[attr-defined]
    result = await tool.execute({}, context=ctx)
    text = result.output
    assert "no dev server" in text.lower()
