"""Route inference from a unified diff — ADR-015 §5/§11 shared helper.

Several lifecycle gates need the same heuristic: given a unified diff,
infer the set of URL routes the changes touched. The PR-reviewer
(Phase 4) introduced this; Phase 5 extracts it to a sibling module so
the complex-flow verify gate, the future heavy reviewer, and any other
gate can call it identically.

Heuristic:

- Any added line matching ``@<router>.get|post|put|patch|delete("...")``
  contributes its path (FastAPI / Starlette decorator).
- Any file added/modified under ``web-next/app/...page.tsx`` contributes
  the URL implied by its directory (Next.js App Router; ``(group)``
  segments are stripped per Next.js convention, ``[param]`` segments
  are kept verbatim — the smoke harness substitutes them later).

A second helper, :func:`is_ui_route`, classifies a route as UI vs API
using a conservative ``not in {/api/, /v1/}`` rule. Anything else is
passed to :func:`verify_primitives.inspect_ui`; UI inspection failures
are advisory when Playwright isn't installed.
"""

from __future__ import annotations

import re

_FASTAPI_DECORATOR_RE = re.compile(
    r'@\w+\.(get|post|put|patch|delete)\(\s*"([^"]+)"',
    re.IGNORECASE,
)
_FASTAPI_DECORATOR_SINGLE_QUOTE_RE = re.compile(
    r"@\w+\.(get|post|put|patch|delete)\(\s*'([^']+)'",
    re.IGNORECASE,
)


def _file_path_from_diff_header(line: str) -> str | None:
    """Return the post-image filename from a '+++ b/<path>' diff header."""

    if not line.startswith("+++ "):
        return None
    rest = line[4:].strip()
    if rest.startswith("b/"):
        return rest[2:]
    if rest == "/dev/null":
        return None
    return rest


def _route_from_nextjs_page_path(path: str) -> str | None:
    """Map a Next.js App Router page path to its URL route.

    Examples:
      - ``web-next/app/(app)/dashboard/page.tsx`` → ``/dashboard``
      - ``web-next/app/repos/[id]/page.tsx`` → ``/repos/[id]``
      - ``web-next/app/page.tsx`` → ``/``

    Returns ``None`` for non-page files.
    """

    if not path.startswith("web-next/app/") or not path.endswith("/page.tsx"):
        return None
    inner = path[len("web-next/app/") : -len("/page.tsx")]
    parts = [p for p in inner.split("/") if not (p.startswith("(") and p.endswith(")"))]
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def infer_routes_from_diff(diff: str) -> list[str]:
    """Return the de-duplicated list of routes touched by ``diff``.

    Order-preserving so tests can assert on the first route.
    """

    routes: list[str] = []
    seen: set[str] = set()

    current_file: str | None = None
    in_hunk = False

    def _add(route: str) -> None:
        if route and route not in seen:
            routes.append(route)
            seen.add(route)

    for raw in (diff or "").splitlines():
        header_path = _file_path_from_diff_header(raw)
        if raw.startswith("+++ "):
            current_file = header_path
            in_hunk = False
            if current_file:
                page_route = _route_from_nextjs_page_path(current_file)
                if page_route:
                    _add(page_route)
            continue
        if raw.startswith("--- "):
            in_hunk = False
            continue
        if raw.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            line = raw[1:]
            for matcher in (_FASTAPI_DECORATOR_RE, _FASTAPI_DECORATOR_SINGLE_QUOTE_RE):
                m = matcher.search(line)
                if m:
                    _add(m.group(2))

    return routes


def is_ui_route(route: str) -> bool:
    """A route is treated as UI if it does not start with an API prefix.

    Conservative: ``/api/...`` and ``/v1/...`` are obviously not UI.
    Anything else (``/dashboard``, ``/`` , ``/widgets``) is treated as
    UI-eligible. Callers pass UI routes to ``inspect_ui``.
    """

    if not route.startswith("/"):
        return False
    api_prefixes = ("/api/", "/v1/")
    return not any(route.startswith(p) for p in api_prefixes)
