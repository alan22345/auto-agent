"""Slow end-to-end smoke test — real Playwright against a tiny http.server.

Validates that ``BrowseUrlTool`` actually drives Chromium, navigates to a real
URL, and emits a screenshot the LLM could reason over. Skipped unless
``-m slow`` is passed.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import socket
import socketserver
import threading

import pytest

from agent.tools.browse_url import BrowseUrlTool


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.slow
async def test_browse_url_drives_real_chromium(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body><h1>Hello smoke</h1>"
        "<p>auto-agent verify/review e2e fixture</p></body></html>"
    )

    port = _free_port()
    directory = str(tmp_path)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, *args, **kwargs):  # silence test output noise
            return

    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    try:
        tool = BrowseUrlTool()
        result = await tool.execute(
            {"url": f"http://127.0.0.1:{port}/"}, context=None,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        # Give the daemon thread a moment to wind down so pytest doesn't grumble.
        await asyncio.sleep(0.05)

    # Output is JSON-packed (Option A from T10) — parse and verify each leg.
    assert result.is_error in (False, None), f"tool reported error: {result.output!r}"
    payload = json.loads(result.output)
    assert payload["http_status"] == 200
    assert "Hello smoke" in payload["text"]
    assert payload["screenshot_media_type"] == "image/png"
    # base64 PNG should at minimum start with iVBORw0KGgo... after decoding
    import base64
    png = base64.b64decode(payload["screenshot_base64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "screenshot is not a PNG"
    assert len(png) > 200  # not an empty image
