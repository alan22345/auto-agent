"""Shared verify primitives — ADR-015 §11.

Four pure functions that every flow (simple, complex, complex_large) calls
to verify a code change at the route, UI, and diff levels:

- :func:`boot_dev_server` — boots the dev server (config-driven via
  ``auto-agent.smoke.yml`` or auto-detected) and returns a handle the
  caller tears down.
- :func:`exercise_routes` — hits each declared route, flags runtime stubs.
- :func:`inspect_ui` — Playwright screenshot + one vision-LLM call.
- :func:`grep_diff_for_stubs` — added-lines-only diff scan for the §8
  forbidden patterns (no-defer enforcement).

The module is intentionally self-contained and side-effect-bounded so the
orchestrator can call it identically from the four gates the ADR
identifies (trio per-item review, complex-flow verify, final review, PR
review). It does **not** integrate with the existing
``agent/lifecycle/verify.py`` flow; that wire-up is a later phase.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import os
import re
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import yaml

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ServerHandle:
    """Lifecycle handle for a dev server boot.

    States:
    - ``"running"``  — subprocess is alive and ``base_url`` is reachable.
    - ``"disabled"`` — no smoke config and no auto-detectable boot command;
      callers treat as "skip smoke."
    - ``"failed"``   — boot was attempted but the health probe never
      returned 200 within ``boot_timeout``. Caller fails the gate.
    """

    state: Literal["running", "disabled", "failed"] = "disabled"
    base_url: str = ""
    port: int = 0
    pid: int | None = None
    pgid: int | None = None
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    # Per-route HTTP POST bodies from smoke.yml; routes not in the map
    # are exercised with GET.
    post_bodies: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per-route expected-shape overrides; if a route is present here the
    # runtime-stub heuristic in :func:`exercise_routes` is bypassed and
    # the declared shape becomes the gate.
    expected_shape: dict[str, str] = field(default_factory=dict)
    failure_reason: str = ""

    @classmethod
    def disabled(cls) -> ServerHandle:
        return cls(state="disabled")

    async def teardown(self) -> None:
        """Kill the dev-server process group, if any. Idempotent."""
        if self.state == "disabled":
            return
        if self.pgid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pgid, signal.SIGTERM)
            if self.process is not None:
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=3.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.pgid, signal.SIGKILL)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.process.wait(), timeout=1.0)
        # Idempotent — clear bookkeeping so a second call is a no-op.
        self.process = None
        self.pid = None
        self.pgid = None
        self.state = "disabled"


@dataclass
class RouteResult:
    ok: bool
    status: int = 0
    body: str = ""
    reason: str = ""


@dataclass
class UIResult:
    ok: bool
    reason: str = ""


@dataclass
class Violation:
    file: str
    line: int
    pattern: str
    snippet: str
    allowed_via_optout: bool = False


@dataclass
class StubResult:
    violations: list[Violation] = field(default_factory=list)


@dataclass
class AllowStubOptout:
    """One ``# auto-agent: allow-stub`` annotation in the PR diff.

    Surfaced in the PR description so a human reviewer (or improvement-
    agent standin) sees the intentional opt-outs at review time —
    ADR-015 §8.
    """

    file: str
    line: int
    snippet: str


# Route alias — kept as a plain str for now (ADR §11: "Route is a string
# (path) for now; future may extend").
Route = str


# ---------------------------------------------------------------------------
# Internal: smoke-config loader & auto-detect fallback
# ---------------------------------------------------------------------------


_SMOKE_FILENAME = "auto-agent.smoke.yml"
_DEFAULT_BOOT_TIMEOUT = 60


def _load_smoke_config(workspace: str) -> dict[str, Any] | None:
    """Return the parsed ``auto-agent.smoke.yml`` dict, or None if absent."""
    path = Path(workspace) / _SMOKE_FILENAME
    if not path.is_file():
        return None
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return None


def _autodetect_boot_command(workspace: str) -> str | None:
    """Fallback boot-command sniff: package.json dev → run.py → make dev.

    Returns ``None`` when nothing matches; caller returns a disabled
    handle in that case.
    """
    ws = Path(workspace)

    pkg = ws / "package.json"
    if pkg.is_file():
        try:
            data = _json.loads(pkg.read_text())
            scripts = data.get("scripts") or {}
            if isinstance(scripts, dict) and "dev" in scripts:
                return "npm run dev"
        except (OSError, ValueError):
            pass

    if (ws / "run.py").is_file():
        return "python3 run.py"

    if (ws / "Makefile").is_file():
        try:
            mk = (ws / "Makefile").read_text()
            if re.search(r"^dev\s*:", mk, re.MULTILINE):
                return "make dev"
        except OSError:
            pass

    return None


def _allocate_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_from_url(url: str) -> int:
    """Extract the port from a health URL, defaulting to 80 / 443."""
    m = re.search(r":(\d+)(/|$)", url)
    if m:
        return int(m.group(1))
    return 443 if url.startswith("https://") else 80


async def _wait_for_health(url: str, *, timeout: float) -> bool:
    """Poll ``url`` until it returns 2xx or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if 200 <= resp.status_code < 300:
                    return True
            except (httpx.HTTPError, OSError):
                pass
            await asyncio.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# Public: boot_dev_server
# ---------------------------------------------------------------------------


async def boot_dev_server(
    *,
    workspace: str,
    default_timeout: int | None = None,
) -> ServerHandle:
    """Boot the project's dev server and return a handle.

    Resolution order:
      1. ``auto-agent.smoke.yml`` at workspace root — its ``boot_command``
         is the source of truth.
      2. Auto-detect: ``package.json`` ``dev`` script → ``run.py`` →
         ``make dev``.
      3. Neither — return :class:`ServerHandle` with ``state="disabled"``;
         callers treat as "skip smoke."

    Side effects: spawns the command as a process group, polls the health
    URL until 2xx or timeout. On timeout the spawned process is killed
    and the handle returned with ``state="failed"``.
    """
    cfg = _load_smoke_config(workspace) or {}
    boot_command = cfg.get("boot_command")
    health_check_url = cfg.get("health_check_url")
    timeout = int(cfg.get("boot_timeout") or default_timeout or _DEFAULT_BOOT_TIMEOUT)
    post_bodies = dict(cfg.get("post_bodies") or {})
    expected_shape = dict(cfg.get("expected_shape") or {})

    if not boot_command:
        boot_command = _autodetect_boot_command(workspace)
        if not boot_command:
            handle = ServerHandle(state="disabled")
            handle.post_bodies = post_bodies
            handle.expected_shape = expected_shape
            return handle
        # Auto-detect path needs a port. We pre-allocate one and surface
        # it via $PORT (mirrors agent/tools/dev_server.py).
        port = _allocate_port()
        health_check_url = health_check_url or f"http://127.0.0.1:{port}/"
        env_port = port
    else:
        # Smoke-config path: the command itself owns the port; we only
        # extract it from the health URL for downstream `exercise_routes`.
        port = _port_from_url(health_check_url or "")
        env_port = port

    env = os.environ.copy()
    env["PORT"] = str(env_port)

    process = await asyncio.create_subprocess_shell(
        boot_command,
        cwd=workspace,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    pgid = os.getpgid(process.pid)

    if health_check_url:
        parsed = urlparse(health_check_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
    else:
        base_url = f"http://127.0.0.1:{port}"
    handle = ServerHandle(
        state="running",
        base_url=base_url,
        port=port,
        pid=process.pid,
        pgid=pgid,
        process=process,
        post_bodies=post_bodies,
        expected_shape=expected_shape,
    )

    healthy = False
    if health_check_url:
        healthy = await _wait_for_health(health_check_url, timeout=float(timeout))
    else:
        # Should not happen — auto-detect path always sets a URL — but
        # guard the branch for safety.
        healthy = await _wait_for_health(f"http://127.0.0.1:{port}/", timeout=float(timeout))

    if not healthy:
        await handle.teardown()
        handle.state = "failed"
        handle.failure_reason = "health_check_timeout"
    return handle


# ---------------------------------------------------------------------------
# Public: exercise_routes
# ---------------------------------------------------------------------------


def _http_client():
    """Indirection used by tests to inject a fake httpx client.

    Production: returns a real ``httpx.AsyncClient`` context manager.
    """
    return httpx.AsyncClient(timeout=10.0)


_RUNTIME_STUB_TRACEBACK_RE = re.compile(r"NotImplementedError", re.IGNORECASE)


def _matches_expected_shape(body_value: Any, shape: str) -> bool:
    """Bare-bones type matcher for smoke.yml ``expected_shape`` declarations.

    Recognised shapes:
      - ``"object"`` — dict (possibly empty)
      - ``"non_empty_object"`` — dict with at least one key
      - ``"array"`` / ``"list"`` — list (possibly empty)
      - ``"non_empty_array"`` — list with len > 0
      - ``"string"``, ``"number"``, ``"bool"``, ``"null"``, ``"any"``

    Unknown shape strings default to ``True`` (don't block on a config
    typo; callers see the declared shape in the smoke report).
    """
    s = (shape or "").strip().lower()
    if s in ("object", "dict"):
        return isinstance(body_value, dict)
    if s in ("non_empty_object", "non_empty_dict"):
        return isinstance(body_value, dict) and len(body_value) > 0
    if s in ("array", "list"):
        return isinstance(body_value, list)
    if s in ("non_empty_array", "non_empty_list"):
        return isinstance(body_value, list) and len(body_value) > 0
    if s == "string":
        return isinstance(body_value, str)
    if s == "number":
        return isinstance(body_value, (int, float)) and not isinstance(body_value, bool)
    if s == "bool":
        return isinstance(body_value, bool)
    if s == "null":
        return body_value is None
    return True


def _looks_like_runtime_stub(body_value: Any, body_text: str, status_code: int) -> bool:
    """Default heuristic for runtime-stub detection (no expected_shape).

    Flags:
      - HTTP 500 whose body mentions ``NotImplementedError`` in a traceback,
      - JSON body of ``null``, ``{}``, or empty list,
      - status >= 400 (any other server error).
    """
    if status_code >= 500 and _RUNTIME_STUB_TRACEBACK_RE.search(body_text or ""):
        return True
    if body_value is None:
        return True
    if isinstance(body_value, dict) and len(body_value) == 0:
        return True
    if isinstance(body_value, list) and len(body_value) == 0:
        return True
    return status_code >= 400


async def exercise_routes(
    routes: list[Route],
    *,
    handle: ServerHandle,
) -> dict[Route, RouteResult]:
    """Hit each route on ``handle.base_url`` and classify the response.

    POST when the route appears in ``handle.post_bodies``; otherwise GET.
    Returns one :class:`RouteResult` per route.
    """
    results: dict[Route, RouteResult] = {}
    if handle.state != "running":
        # Disabled / failed handles can't be exercised; the caller should
        # have decided whether to gate on this earlier.
        for r in routes:
            results[r] = RouteResult(ok=False, reason=f"server_{handle.state}")
        return results

    async with _http_client() as client:
        for route in routes:
            url = handle.base_url.rstrip("/") + "/" + route.lstrip("/")
            try:
                if route in handle.post_bodies:
                    resp = await client.post(url, json=handle.post_bodies[route])
                else:
                    resp = await client.get(url)
            except Exception as exc:
                results[route] = RouteResult(ok=False, reason=f"request_error: {exc}")
                continue

            status = getattr(resp, "status_code", 0)
            text = getattr(resp, "text", "") or ""
            try:
                body_value = resp.json()
            except Exception:
                body_value = None

            # Per-route expected_shape override takes precedence over heuristic.
            if route in handle.expected_shape:
                if 200 <= status < 300 and _matches_expected_shape(
                    body_value, handle.expected_shape[route]
                ):
                    results[route] = RouteResult(ok=True, status=status, body=text)
                else:
                    results[route] = RouteResult(
                        ok=False,
                        status=status,
                        body=text,
                        reason="expected_shape_mismatch",
                    )
                continue

            if 200 <= status < 300 and not _looks_like_runtime_stub(body_value, text, status):
                results[route] = RouteResult(ok=True, status=status, body=text)
            else:
                results[route] = RouteResult(
                    ok=False,
                    status=status,
                    body=text,
                    reason="runtime_stub_shape",
                )

    return results


# ---------------------------------------------------------------------------
# Public: inspect_ui
# ---------------------------------------------------------------------------


async def _screenshot_route(*, url: str) -> bytes:
    """Take a full-page PNG screenshot of ``url`` using Playwright.

    Raises :class:`ImportError` if Playwright is not installed; the
    caller in :func:`inspect_ui` converts that to a graceful
    ``playwright_not_installed`` result so the gate doesn't crash on
    repos where the dependency hasn't been provisioned yet.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ImportError("playwright not installed") from exc

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            png_bytes = await page.screenshot(full_page=True)
            await ctx.close()
        finally:
            await browser.close()
    return png_bytes


_UI_JUDGE_SYSTEM = (
    "You are a UI-verification judge. Given a screenshot and a stated user "
    "intent, decide if the UI matches the intent. Reply with JSON only: "
    '{"verdict": "PASS" | "FAIL", "reason": "<one-sentence reason>"}.'
)


async def _judge_screenshot(
    *,
    screenshot: bytes,
    intent: str,
    route: str,
) -> dict[str, Any]:
    """One LLM call returning ``{verdict, reason}``.

    The wire shape for sending the screenshot is deliberately
    encapsulated here so tests can mock it. Production callers go
    through ``agent.llm.structured.complete_json`` with a Message whose
    content includes a base64 data URI of the PNG; once the LLM seam
    grows native image blocks (see ``agent/tools/browse_url.py`` TODO),
    this is the seam to upgrade.
    """
    import base64

    from agent.llm import get_provider
    from agent.llm.structured import complete_json
    from agent.llm.types import Message

    encoded = base64.b64encode(screenshot).decode("ascii")
    # Phase 1 keeps the screenshot inline as a data URI in the user
    # message text. This is suboptimal for token cost but unblocks the
    # primitive; the LLM seam upgrade to native image blocks is a Phase 7
    # concern (matches the browse_url TODO).
    user_text = (
        f"Route under test: {route}\n"
        f"Stated user intent: {intent}\n\n"
        f"Screenshot (base64-encoded PNG, {len(screenshot)} bytes):\n"
        f"data:image/png;base64,{encoded}\n\n"
        "Does this UI match the stated intent? Reply with JSON only."
    )
    provider = get_provider()
    return await complete_json(
        provider,
        messages=[Message(role="user", content=user_text)],
        system=_UI_JUDGE_SYSTEM,
        max_tokens=512,
    )


async def inspect_ui(
    *,
    route: str,
    intent: str,
    base_url: str,
) -> UIResult:
    """Screenshot ``base_url + route`` and ask the model to PASS/FAIL it.

    Gracefully degrades:
      - Playwright import failure → ``UIResult(ok=False, reason="playwright_not_installed")``.
      - LLM judge errors (parse failure, network) → ``UIResult(ok=False, reason="judge_error: <detail>")``.
    """
    url = base_url.rstrip("/") + "/" + route.lstrip("/")
    try:
        screenshot = await _screenshot_route(url=url)
    except ImportError:
        return UIResult(ok=False, reason="playwright_not_installed")
    except Exception as exc:
        return UIResult(ok=False, reason=f"screenshot_error: {exc}")

    try:
        verdict_payload = await _judge_screenshot(
            screenshot=screenshot,
            intent=intent,
            route=route,
        )
    except Exception as exc:
        return UIResult(ok=False, reason=f"judge_error: {exc}")

    verdict = str(verdict_payload.get("verdict", "")).strip().upper()
    reason = str(verdict_payload.get("reason", "")).strip()
    if verdict == "PASS":
        return UIResult(ok=True, reason=reason or "looks correct")
    return UIResult(ok=False, reason=reason or "verdict=FAIL")


# ---------------------------------------------------------------------------
# Public: grep_diff_for_stubs (ADR-015 §8 layer 3)
# ---------------------------------------------------------------------------


# Patterns to flag on an ADDED line. Each entry is (label, compiled_regex).
# The regex is matched against the added-line content (after the leading
# ``+`` is stripped). Ordering is by specificity — more concrete patterns
# first, broader backlog-text phrases last.
#
# Phase 9 (ADR-015 §8) extends the original set with the variants
# adversarial agents emit to dodge the obvious ``# Phase N`` form:
# the hyphen variant (``# Phase-N``), the lowercase variant
# (``# phase N`` / ``# phase-N``), and the colon-suffixed variant
# (``# Phase N:`` / ``# Phase-N:``). Hyphen and colon variants are
# folded into the same regex (``\s*-?\s*\d+\s*:?``) so each maps to one
# pattern label.
_STUB_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("raise NotImplementedError", re.compile(r"\braise\s+NotImplementedError\b")),
    ("# TODO(phase", re.compile(r"#\s*TODO\(\s*phase\b", re.IGNORECASE)),
    ("pass  # placeholder", re.compile(r"\bpass\b\s\s#\s*placeholder\b", re.IGNORECASE)),
    # Combined Phase pattern — covers ``# Phase 1``, ``# Phase-1``,
    # ``# phase 1``, ``# phase-1``, ``# Phase 1:``, ``# Phase-1:`` (and
    # their lowercase versions). The optional hyphen + optional colon
    # suffix make this one regex match every variant.
    ("# Phase N", re.compile(r"#\s*Phase\s*-?\s*\d+\s*:?", re.IGNORECASE)),
    ("# v2 will", re.compile(r"#\s*v2\s+will\b", re.IGNORECASE)),
    ("# in a future PR", re.compile(r"#\s*in\s+a\s+future\s+PR\b", re.IGNORECASE)),
    ("Phase 1", re.compile(r"\bPhase\s+1\b")),
    ("v2 ships", re.compile(r"\bv2\s+ships\b", re.IGNORECASE)),
    (
        "will be implemented later",
        re.compile(r"\bwill\s+be\s+implemented\s+later\b", re.IGNORECASE),
    ),
    ("for now this is a stub", re.compile(r"\bfor\s+now\s+this\s+is\s+a\s+stub\b", re.IGNORECASE)),
)


_OPTOUT_SUFFIX = "# auto-agent: allow-stub"


def _path_excluded(path: str) -> bool:
    """Test/markdown paths are out of scope for stub-grep (ADR-015 §8)."""
    if path.startswith("tests/") or "/tests/" in path:
        return True
    name = path.rsplit("/", 1)[-1]
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return path.endswith((".md", ".mdx"))


_HUNK_HEADER_RE = re.compile(r"^@@\s")


def grep_diff_for_stubs(diff: str) -> StubResult:
    """Scan ADDED LINES of a unified diff for forbidden stub patterns.

    See ADR-015 §8 (no-defer enforcement, layer 3). The function reports
    every match; the orchestrator decides whether to block by filtering
    ``[v for v in result.violations if not v.allowed_via_optout]``.
    """
    violations: list[Violation] = []

    current_file: str | None = None
    new_line_no: int = 0
    in_hunk = False

    for raw in (diff or "").splitlines():
        # File header — capture the post-image path (the `b/` side).
        if raw.startswith("+++ "):
            # Lines look like '+++ b/path/to/file.py' or '+++ /dev/null'.
            rest = raw[4:].strip()
            if rest.startswith("b/"):
                current_file = rest[2:]
            elif rest == "/dev/null":
                current_file = None
            else:
                current_file = rest
            in_hunk = False
            continue

        # Discard the matching '--- a/...' header (we use the +++ side).
        if raw.startswith("--- "):
            in_hunk = False
            continue

        if _HUNK_HEADER_RE.match(raw):
            # Parse '@@ -a,b +c,d @@' to seed the new-side line counter.
            m = re.search(r"\+(\d+)(?:,\d+)?\s", raw)
            new_line_no = int(m.group(1)) if m else 1
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if raw.startswith("+") and not raw.startswith("+++"):
            line_content = raw[1:]
            # Path-excluded files emit no violations at all.
            if current_file and _path_excluded(current_file):
                new_line_no += 1
                continue

            opted_out = line_content.rstrip().endswith(_OPTOUT_SUFFIX)
            for label, pattern in _STUB_PATTERNS:
                if pattern.search(line_content):
                    violations.append(
                        Violation(
                            file=current_file or "<unknown>",
                            line=new_line_no,
                            pattern=label,
                            snippet=line_content.rstrip("\n"),
                            allowed_via_optout=opted_out,
                        )
                    )
                    # One violation per added line is enough — the first
                    # match labels it; we don't want N violations for a
                    # line that happens to hit multiple patterns.
                    break
            new_line_no += 1
        elif raw.startswith("-"):
            # Removed line on the old side — does not advance the new-side counter.
            continue
        else:
            # Context line.
            new_line_no += 1

    return StubResult(violations=violations)


# ---------------------------------------------------------------------------
# Public: allow-stub surfacing helpers (ADR-015 §8)
# ---------------------------------------------------------------------------


def collect_allow_stub_optouts(diff: str) -> list[AllowStubOptout]:
    """Walk a unified diff and return every ``# auto-agent: allow-stub`` line.

    Used by the PR-creation path to surface intentional stubs in the PR
    description, and by the PR-reviewer artefact scope to decide whether
    a stub-grep hit should block (no opt-out) or surface only (opt-out).

    The path-exclusion rules (tests/markdown) DO NOT apply here — an
    allow-stub annotation on a markdown line is still an explicit
    opt-out the human should see in the PR body.
    """

    optouts: list[AllowStubOptout] = []
    current_file: str | None = None
    new_line_no: int = 0
    in_hunk = False

    for raw in (diff or "").splitlines():
        if raw.startswith("+++ "):
            rest = raw[4:].strip()
            if rest.startswith("b/"):
                current_file = rest[2:]
            elif rest == "/dev/null":
                current_file = None
            else:
                current_file = rest
            in_hunk = False
            continue
        if raw.startswith("--- "):
            in_hunk = False
            continue
        if _HUNK_HEADER_RE.match(raw):
            m = re.search(r"\+(\d+)(?:,\d+)?\s", raw)
            new_line_no = int(m.group(1)) if m else 1
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            line_content = raw[1:]
            if line_content.rstrip().endswith(_OPTOUT_SUFFIX):
                optouts.append(
                    AllowStubOptout(
                        file=current_file or "<unknown>",
                        line=new_line_no,
                        snippet=line_content.rstrip("\n"),
                    )
                )
            new_line_no += 1
        elif raw.startswith("-"):
            continue
        else:
            new_line_no += 1
    return optouts


_ALLOW_STUB_SECTION_HEADER = "## Allow-stub opt-outs in this PR"


def format_allow_stub_section(optouts: list[AllowStubOptout]) -> str:
    """Render the markdown section appended to PR bodies — ADR-015 §8.

    Empty list → empty string (PRs without allow-stub get no section).
    Each opt-out becomes one bullet ``- <file>:<line> — <snippet>``.
    """

    if not optouts:
        return ""
    lines = [
        _ALLOW_STUB_SECTION_HEADER,
        "",
        (
            "These lines were stubbed intentionally with an explicit opt-out "
            "from the no-defer enforcement (ADR-015 §8). A human (or the "
            "improvement-agent standin) should confirm each is justified."
        ),
        "",
    ]
    for o in optouts:
        snippet = o.snippet.strip()
        lines.append(f"- `{o.file}:{o.line}` — `{snippet}`")
    return "\n".join(lines)


def augment_pr_body_with_optouts(body: str, diff: str) -> str:
    """Append the allow-stub section to ``body`` when ``diff`` carries any.

    A no-op when the diff has zero opt-outs — keeps the PR body identical
    so clean PRs don't gain a noisy empty section.
    """

    optouts = collect_allow_stub_optouts(diff)
    section = format_allow_stub_section(optouts)
    if not section:
        return body
    suffix = "\n\n" + section if not body.endswith("\n") else "\n" + section
    return body + suffix
