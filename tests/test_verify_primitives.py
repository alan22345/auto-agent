"""Spec for ``agent.lifecycle.verify_primitives`` — ADR-015 §11.

Four pure primitives that every flow (simple, complex, complex_large) calls:

- ``boot_dev_server() -> ServerHandle`` — boot the dev server (config-driven
  via ``auto-agent.smoke.yml`` or auto-detected) and return a handle that
  the caller tears down.
- ``exercise_routes(routes) -> dict[Route, RouteResult]`` — GET/POST each
  route, flag runtime stubs (``{}``/``null``/empty list/500 with
  ``NotImplementedError``). Per-route ``expected_shape`` overrides the
  default heuristic.
- ``inspect_ui(route, intent) -> UIResult`` — Playwright screenshot + one
  vision-LLM call returning PASS/FAIL + reason. Gracefully errors if
  Playwright is missing.
- ``grep_diff_for_stubs(diff) -> StubResult`` — added-lines-only scan of a
  unified diff for forbidden stub patterns (§8), honouring the
  ``# auto-agent: allow-stub`` opt-out and excluding tests/markdown.

All four are pure and side-effect-bounded so the orchestrator can call
them from any of the four gates the ADR identifies (trio per-item review,
complex-flow ``verify.py``, final review, PR review).
"""

from __future__ import annotations

import socket
import textwrap
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# boot_dev_server
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_TINY_SERVER = """\
import http.server, socketserver, os, sys
port = int(os.environ['PORT']) if 'PORT' in os.environ else int(sys.argv[1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok": true}')
    def log_message(self, *a, **kw):
        pass
with socketserver.TCPServer(('127.0.0.1', port), H) as s:
    s.serve_forever()
"""


@pytest.mark.asyncio
async def test_boot_reads_smoke_yml(tmp_path: Path):
    from agent.lifecycle.verify_primitives import boot_dev_server

    port = _find_free_port()
    (tmp_path / "srv.py").write_text(_TINY_SERVER)
    (tmp_path / "auto-agent.smoke.yml").write_text(
        textwrap.dedent(
            f"""\
            boot_command: "python3 srv.py {port}"
            health_check_url: "http://127.0.0.1:{port}/"
            boot_timeout: 10
            """
        )
    )

    handle = await boot_dev_server(workspace=str(tmp_path))
    try:
        assert handle.state == "running"
        assert handle.port == port
        # Sanity: the server is actually listening.
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    finally:
        await handle.teardown()


@pytest.mark.asyncio
async def test_boot_autodetects_run_py(tmp_path: Path):
    """No smoke.yml → fallback to package.json → run.py → make dev order.

    The impl allocates a port and exposes it via ``$PORT``; the spawned
    ``run.py`` reads it. We assert ``state == "running"`` and that the
    handle's port is reachable rather than equal to a specific number.
    """
    from agent.lifecycle.verify_primitives import boot_dev_server

    # Auto-detect picks `python3 run.py` from the run.py rung. We make
    # run.py be the tiny server so the auto-detect command actually boots.
    # The server reads PORT from env (set by boot_dev_server).
    (tmp_path / "run.py").write_text(_TINY_SERVER)
    handle = await boot_dev_server(workspace=str(tmp_path), default_timeout=10)
    try:
        assert handle.state == "running"
        assert handle.port > 0
        with socket.create_connection(("127.0.0.1", handle.port), timeout=2):
            pass
    finally:
        await handle.teardown()


@pytest.mark.asyncio
async def test_boot_returns_disabled_when_no_config_and_no_autodetect(tmp_path: Path):
    """Empty workspace → ServerHandle.state == 'disabled' (callers skip smoke)."""
    from agent.lifecycle.verify_primitives import boot_dev_server

    handle = await boot_dev_server(workspace=str(tmp_path))
    assert handle.state == "disabled"
    # teardown on a disabled handle is a no-op (idempotent).
    await handle.teardown()


@pytest.mark.asyncio
async def test_teardown_kills_subprocess(tmp_path: Path):
    from agent.lifecycle.verify_primitives import boot_dev_server

    port = _find_free_port()
    (tmp_path / "srv.py").write_text(_TINY_SERVER)
    (tmp_path / "auto-agent.smoke.yml").write_text(
        textwrap.dedent(
            f"""\
            boot_command: "python3 srv.py {port}"
            health_check_url: "http://127.0.0.1:{port}/"
            boot_timeout: 10
            """
        )
    )

    handle = await boot_dev_server(workspace=str(tmp_path))
    assert handle.state == "running"
    await handle.teardown()

    # Port should refuse after teardown.
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        pass


# ---------------------------------------------------------------------------
# exercise_routes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or ("" if json_data is None else __import__("json").dumps(json_data))

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


class _FakeAsyncClient:
    """Minimal httpx-style stand-in for testing exercise_routes."""

    def __init__(self, routes: dict[str, _FakeResponse]):
        # keys are route paths (e.g. '/api/foo'); we match by suffix.
        self._routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url, **kwargs):
        return self._lookup(url)

    async def post(self, url, **kwargs):
        return self._lookup(url)

    def _lookup(self, url: str) -> _FakeResponse:
        for path, resp in self._routes.items():
            if url.endswith(path):
                return resp
        raise AssertionError(f"unexpected URL in test: {url}")


@pytest.mark.asyncio
async def test_exercise_routes_2xx_pass():
    from agent.lifecycle.verify_primitives import ServerHandle, exercise_routes

    handle = ServerHandle.disabled()
    handle.base_url = "http://127.0.0.1:9999"
    handle.state = "running"

    fake = _FakeAsyncClient(
        {
            "/api/healthy": _FakeResponse(status_code=200, json_data={"ok": True, "data": [1]}),
        }
    )
    with patch("agent.lifecycle.verify_primitives._http_client", lambda: fake):
        results = await exercise_routes(["/api/healthy"], handle=handle)

    assert results["/api/healthy"].ok is True
    assert results["/api/healthy"].status == 200


@pytest.mark.asyncio
async def test_exercise_routes_500_with_notimplemented_flags_stub():
    from agent.lifecycle.verify_primitives import ServerHandle, exercise_routes

    handle = ServerHandle.disabled()
    handle.base_url = "http://127.0.0.1:9999"
    handle.state = "running"

    fake = _FakeAsyncClient(
        {
            "/api/broken": _FakeResponse(
                status_code=500,
                text="Internal Server Error\nTraceback (most recent call last):\n  raise NotImplementedError\n",
            ),
        }
    )
    with patch("agent.lifecycle.verify_primitives._http_client", lambda: fake):
        results = await exercise_routes(["/api/broken"], handle=handle)

    r = results["/api/broken"]
    assert r.ok is False
    assert r.reason == "runtime_stub_shape"


@pytest.mark.asyncio
async def test_exercise_routes_empty_object_flags_stub():
    from agent.lifecycle.verify_primitives import ServerHandle, exercise_routes

    handle = ServerHandle.disabled()
    handle.base_url = "http://127.0.0.1:9999"
    handle.state = "running"

    fake = _FakeAsyncClient(
        {
            "/api/empty": _FakeResponse(status_code=200, json_data={}),
        }
    )
    with patch("agent.lifecycle.verify_primitives._http_client", lambda: fake):
        results = await exercise_routes(["/api/empty"], handle=handle)

    r = results["/api/empty"]
    assert r.ok is False
    assert r.reason == "runtime_stub_shape"


@pytest.mark.asyncio
async def test_exercise_routes_expected_shape_overrides_heuristic(tmp_path: Path):
    """smoke.yml's expected_shape for a route beats the empty-object heuristic."""
    from agent.lifecycle.verify_primitives import ServerHandle, exercise_routes

    handle = ServerHandle.disabled()
    handle.base_url = "http://127.0.0.1:9999"
    handle.state = "running"
    # Per-route override: this endpoint is *expected* to return {}; pass it.
    handle.expected_shape = {"/api/empty": "object"}

    fake = _FakeAsyncClient(
        {
            "/api/empty": _FakeResponse(status_code=200, json_data={}),
        }
    )
    with patch("agent.lifecycle.verify_primitives._http_client", lambda: fake):
        results = await exercise_routes(["/api/empty"], handle=handle)

    assert results["/api/empty"].ok is True


# ---------------------------------------------------------------------------
# inspect_ui
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_ui_returns_playwright_not_installed_when_missing():
    """When Playwright isn't importable, return UIResult(ok=False) gracefully."""
    from agent.lifecycle import verify_primitives as vp

    # Force the import-probe to look as if Playwright isn't installed.
    with patch.object(
        vp, "_screenshot_route", side_effect=ImportError("no module named 'playwright'")
    ):
        result = await vp.inspect_ui(
            route="/dashboard",
            intent="User can see project list",
            base_url="http://localhost:3000",
        )
    assert result.ok is False
    assert result.reason == "playwright_not_installed"


@pytest.mark.asyncio
async def test_inspect_ui_pass_verdict():
    """Mocked vision-LLM PASS → UIResult.ok=True with reason carried through."""
    from agent.lifecycle import verify_primitives as vp

    fake_png = b"\x89PNG\r\n\x1a\n" + b"fake-pixels"
    with (
        patch.object(vp, "_screenshot_route", AsyncMock(return_value=fake_png)) as screenshot,
        patch.object(
            vp,
            "_judge_screenshot",
            AsyncMock(return_value={"verdict": "PASS", "reason": "Looks fine"}),
        ) as judge,
    ):
        result = await vp.inspect_ui(
            route="/dashboard",
            intent="User can see project list",
            base_url="http://localhost:3000",
        )
    assert result.ok is True
    assert "fine" in result.reason.lower()
    screenshot.assert_awaited_once()
    # Confirm the judge was handed the actual screenshot bytes.
    judge_kwargs = judge.await_args.kwargs or {}
    judge_args = judge.await_args.args
    assert (judge_kwargs.get("screenshot") if judge_kwargs else judge_args[0]) == fake_png


@pytest.mark.asyncio
async def test_inspect_ui_fail_verdict():
    """Mocked vision-LLM FAIL → UIResult.ok=False with reason."""
    from agent.lifecycle import verify_primitives as vp

    fake_png = b"\x89PNG\r\n\x1a\n" + b"more-fake-pixels"
    with (
        patch.object(vp, "_screenshot_route", AsyncMock(return_value=fake_png)),
        patch.object(
            vp,
            "_judge_screenshot",
            AsyncMock(return_value={"verdict": "FAIL", "reason": "Empty list shown"}),
        ),
    ):
        result = await vp.inspect_ui(
            route="/dashboard",
            intent="User can see project list",
            base_url="http://localhost:3000",
        )
    assert result.ok is False
    assert "empty" in result.reason.lower()


# ---------------------------------------------------------------------------
# grep_diff_for_stubs
# ---------------------------------------------------------------------------


def _make_diff(path: str, added_lines: list[str], context: list[str] | None = None) -> str:
    """Build a minimal unified diff hunk for one file with added lines.

    ``added_lines`` are emitted as ``+`` lines; ``context`` (if given) is
    emitted unchanged.
    """
    body_lines = []
    if context:
        for c in context:
            body_lines.append(f" {c}")
    for a in added_lines:
        body_lines.append(f"+{a}")
    body = "\n".join(body_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,1 +1,{len(added_lines) + len(context or [])} @@\n"
        f"{body}\n"
    )


def test_grep_catches_not_implemented_error():
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    diff = _make_diff("src/foo.py", ["    raise NotImplementedError"])
    result = grep_diff_for_stubs(diff)
    real = [v for v in result.violations if not v.allowed_via_optout]
    assert len(real) == 1
    assert real[0].file == "src/foo.py"
    assert "NotImplementedError" in real[0].pattern


def test_grep_ignores_unchanged_lines():
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    # Same forbidden phrase, but as a context line (unchanged).
    diff = _make_diff(
        "src/foo.py",
        added_lines=["    return None"],
        context=["    raise NotImplementedError"],
    )
    result = grep_diff_for_stubs(diff)
    real = [v for v in result.violations if not v.allowed_via_optout]
    assert real == []


def test_grep_honors_allow_stub_optout():
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    diff = _make_diff(
        "src/foo.py",
        ["    raise NotImplementedError  # auto-agent: allow-stub"],
    )
    result = grep_diff_for_stubs(diff)
    # Should show up in `violations` but flagged as allowed.
    assert len(result.violations) == 1
    assert result.violations[0].allowed_via_optout is True
    real = [v for v in result.violations if not v.allowed_via_optout]
    assert real == []


def test_grep_excludes_test_paths():
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    diff_test = _make_diff("tests/test_foo.py", ["    raise NotImplementedError"])
    diff_test2 = _make_diff("backend/test_bar.py", ["    raise NotImplementedError"])
    diff_md = _make_diff("docs/notes.md", ["TODO(phase 2): write this section"])
    diff_mdx = _make_diff("web-next/page.mdx", ["raise NotImplementedError"])

    for d in (diff_test, diff_test2, diff_md, diff_mdx):
        r = grep_diff_for_stubs(d)
        real = [v for v in r.violations if not v.allowed_via_optout]
        assert real == [], f"should not have flagged: {d!r}"


def test_grep_catches_todo_phase_and_phase_number_comments():
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    diff_todo = _make_diff("src/a.py", ["    # TODO(phase 1): implement tomorrow"])
    diff_phase = _make_diff("src/b.py", ["    # Phase 2: real impl"])
    diff_v2 = _make_diff("src/c.py", ["    # v2 will fix this"])
    diff_future = _make_diff("src/d.py", ["    # in a future PR"])
    diff_placeholder = _make_diff("src/e.py", ["    pass  # placeholder"])
    diff_for_now = _make_diff("src/f.py", ["    # for now this is a stub"])

    for d in (diff_todo, diff_phase, diff_v2, diff_future, diff_placeholder, diff_for_now):
        r = grep_diff_for_stubs(d)
        real = [v for v in r.violations if not v.allowed_via_optout]
        assert len(real) == 1, f"expected 1 violation in {d!r}, got {r.violations!r}"


def test_grep_handles_multi_file_diff():
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    d1 = _make_diff("a.py", ["    raise NotImplementedError"])
    d2 = _make_diff("b.py", ["    # TODO(phase 3): later"])
    combined = d1 + d2

    result = grep_diff_for_stubs(combined)
    real = [v for v in result.violations if not v.allowed_via_optout]
    files = {v.file for v in real}
    assert files == {"a.py", "b.py"}
    assert len(real) == 2


def test_grep_catches_phase_hyphen_variant():
    """The hyphen variant ``# Phase-N`` must be caught — Phase 4 evasion lesson.

    Adversarial agents may emit ``# Phase-2`` to dodge the original
    ``# Phase 2`` regex; the hyphen variant must be flagged identically.
    """
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    diff_hyphen = _make_diff("src/a.py", ["    # Phase-2 fills this in"])
    diff_hyphen_no_space = _make_diff("src/b.py", ["    # Phase-1"])

    for d in (diff_hyphen, diff_hyphen_no_space):
        r = grep_diff_for_stubs(d)
        real = [v for v in r.violations if not v.allowed_via_optout]
        assert len(real) == 1, f"expected 1 violation in {d!r}, got {r.violations!r}"


def test_grep_catches_lowercase_phase_variant():
    """``# phase N`` lowercase must be caught — case-insensitive sweep."""
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    diff_lower = _make_diff("src/a.py", ["    # phase 2: implement later"])
    diff_lower_hyphen = _make_diff("src/b.py", ["    # phase-3 follow-up"])

    for d in (diff_lower, diff_lower_hyphen):
        r = grep_diff_for_stubs(d)
        real = [v for v in r.violations if not v.allowed_via_optout]
        assert len(real) == 1, f"expected 1 violation in {d!r}, got {r.violations!r}"


def test_grep_catches_phase_colon_variants():
    """``# Phase N:`` and ``# Phase-N:`` (colon suffix) must be caught."""
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    diff_colon = _make_diff("src/a.py", ["    # Phase 2: real impl coming"])
    diff_colon_hyphen = _make_diff("src/b.py", ["    # Phase-2: real impl coming"])

    for d in (diff_colon, diff_colon_hyphen):
        r = grep_diff_for_stubs(d)
        real = [v for v in r.violations if not v.allowed_via_optout]
        assert len(real) == 1, f"expected 1 violation in {d!r}, got {r.violations!r}"


def test_grep_ignores_plus_plus_plus_header():
    """The ``+++ b/path`` line is a diff header, not an added code line."""
    from agent.lifecycle.verify_primitives import grep_diff_for_stubs

    # A header line that mentions a forbidden phrase in the path itself.
    # We construct it by hand because _make_diff already emits a clean header.
    diff = textwrap.dedent(
        """\
        diff --git a/raise_NotImplementedError_file.py b/raise_NotImplementedError_file.py
        --- a/raise_NotImplementedError_file.py
        +++ b/raise_NotImplementedError_file.py
        @@ -1,1 +1,1 @@
        +x = 1
        """
    )
    result = grep_diff_for_stubs(diff)
    real = [v for v in result.violations if not v.allowed_via_optout]
    assert real == []
