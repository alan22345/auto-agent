"""Tests for dev-server log file cleanup — loose-end #1."""

from __future__ import annotations

import asyncio
import os
import time

from agent.tools.dev_server import DevServerHandle, kill_server


async def test_kill_server_unlinks_log_file(tmp_path):
    """kill_server must remove the dev-server log file."""
    log_file = tmp_path / "dev-server-test.log"
    log_file.write_text("boot output\n")

    # Spawn a trivial process that exits immediately.
    proc = await asyncio.create_subprocess_exec(
        "true",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )

    handle = DevServerHandle(
        pid=proc.pid,
        pgid=os.getpgid(proc.pid),
        port=0,
        log_path=str(log_file),
        started_at=time.time(),
        process=proc,
    )

    await kill_server(handle)

    assert not os.path.isfile(str(log_file)), (
        f"Log file at {log_file} was not unlinked by kill_server"
    )


async def test_kill_server_tolerates_missing_log(tmp_path):
    """kill_server must not raise if the log file was already removed."""
    proc = await asyncio.create_subprocess_exec(
        "true",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )

    handle = DevServerHandle(
        pid=proc.pid,
        pgid=os.getpgid(proc.pid),
        port=0,
        log_path=str(tmp_path / "nonexistent.log"),
        started_at=time.time(),
        process=proc,
    )

    # Should not raise even though the file doesn't exist.
    await kill_server(handle)
