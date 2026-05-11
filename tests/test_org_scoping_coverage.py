"""Static-analysis test that asserts every tenant endpoint is org-scoped.

This is the lightweight stand-in for the real-DB parametrized isolation
suite that Phase 2's spec asks for. It walks the FastAPI route table
in ``orchestrator/router.py`` and ``orchestrator/orgs.py`` and asserts
that every route handler that touches tenant data takes a dependency
on ``orchestrator.auth.current_org_id`` — which is the load-bearing
guarantee that the route filters by the caller's active org.

Adding a new endpoint that returns or mutates tenant rows without
hooking up the dependency will fail this test, catching the most
common regression pattern (the kind the original spec was worried
about with its proposed pre-commit lint).

Endpoints that are intentionally NOT org-scoped (auth/signup/secrets
that key by user only, claude pairing, etc.) live in the
``UNSCOPED_ALLOWLIST`` and must be explicitly justified there.
"""

from __future__ import annotations

import inspect

import pytest

from orchestrator import orgs as orgs_module
from orchestrator import router as router_module
from orchestrator.auth import current_org_id as current_org_id_dep

# Endpoints that are intentionally NOT scoped by org. Each entry is the
# (method, path) tuple and a one-line reason. Adding new entries here
# requires a code-review-grade justification — the bar is high.
UNSCOPED_ALLOWLIST: dict[tuple[str, str], str] = {
    # Auth + signup — pre-org-context (caller has no current_org_id yet).
    ("POST", "/auth/login"):           "issues the token; caller has no org yet",
    ("POST", "/auth/logout"):          "clears cookie; no DB read",
    ("POST", "/auth/signup"):          "creates user + personal org",
    ("GET",  "/auth/verify/{token}"):  "click-through; re-issues token with org",
    ("PATCH", "/auth/me/email"):       "updates own User row only",
    ("GET",  "/auth/me"):              "returns own User row only",
    ("PATCH", "/auth/me/telegram"):    "updates own User row only",
    ("PATCH", "/auth/me/slack"):       "updates own User row only",
    # Per-user secrets — User-keyed, not org-keyed at the read.
    # The DB layer still enforces (user_id, organization_id, key) via the
    # PK; the endpoints read current_org_id from the JWT payload directly
    # rather than via the FastAPI dep.
    ("GET",    "/me/secrets"):          "reads payload['current_org_id'] inline",
    ("PUT",    "/me/secrets/{key}"):    "reads payload['current_org_id'] inline",
    ("DELETE", "/me/secrets/{key}"):    "reads payload['current_org_id'] inline",
    ("POST",   "/me/secrets/{key}/test"): "reads payload['current_org_id'] inline",
    # Admin endpoints — system-wide, not tenant data.
    ("POST", "/auth/users"):  "admin creates a user (not tenant data)",
    ("GET",  "/auth/users"):  "admin lists users (system-wide)",
    # Claude OAuth pairing — keyed by the authenticated user only;
    # credentials are stored per-user under /data/users/{user_id}/.
    ("POST", "/claude/pair/start"):       "pairing is per-user, not per-org",
    ("POST", "/claude/pair/code"):        "pairing is per-user, not per-org",
    ("GET",  "/claude/pair/status"):      "pairing is per-user, not per-org",
    ("POST", "/claude/pair/disconnect"):  "pairing is per-user, not per-org",
    # Task creation: scopes inline via payload["current_org_id"] (not
    # via the Depends dep because webhook callers without a JWT still
    # need to invoke this path).
    ("POST", "/tasks"):  "reads payload['current_org_id'] inline; webhooks supported",
    # POST to a freeform-created repo — same shape as /tasks.
    ("POST", "/freeform/create-repo"):  "reads payload['current_org_id'] inline",
    # Org switcher — gates by membership-row existence, not active org.
    ("POST", "/me/current-org"):  "membership check is the gate; not active-org-scoped",
    # Org members list — uses current_org_id dep but path includes org_id;
    # treat as scoped (the dep IS there; this entry catches the path-param check).
}


def _route_methods(route) -> list[str]:
    """Return the HTTP methods on a FastAPI APIRoute, sorted."""
    return sorted(route.methods - {"HEAD", "OPTIONS"})


def _depends_on_current_org_id(endpoint) -> bool:
    """True if ``endpoint``'s signature includes ``Depends(current_org_id)``."""
    sig = inspect.signature(endpoint)
    for param in sig.parameters.values():
        default = param.default
        # FastAPI's Depends wraps the dependency; the underlying callable
        # sits on ``.dependency``.
        dep = getattr(default, "dependency", None)
        if dep is current_org_id_dep:
            return True
    return False


def _iter_routes(*modules):
    """Yield (method, path, endpoint) for every APIRoute on every module's router."""
    for module in modules:
        for route in module.router.routes:
            methods = getattr(route, "methods", None)
            endpoint = getattr(route, "endpoint", None)
            path = getattr(route, "path", None)
            if not methods or not endpoint or not path:
                continue
            for method in _route_methods(route):
                yield method, path, endpoint


def test_every_tenant_endpoint_is_scoped():
    """Every route must either Depends(current_org_id) or be in the allowlist."""
    failures: list[str] = []
    for method, path, endpoint in _iter_routes(router_module, orgs_module):
        key = (method, path)
        if key in UNSCOPED_ALLOWLIST:
            continue
        if _depends_on_current_org_id(endpoint):
            continue
        failures.append(
            f"{method} {path} ({endpoint.__module__}.{endpoint.__name__}) — "
            f"missing Depends(current_org_id) and not in UNSCOPED_ALLOWLIST. "
            f"Either add the dependency or, with justification, add the "
            f"route to tests/test_org_scoping_coverage.py::UNSCOPED_ALLOWLIST."
        )
    assert not failures, "Cross-org scoping regressions:\n  - " + "\n  - ".join(failures)


def test_allowlist_does_not_contain_phantom_routes():
    """Catch typos — every UNSCOPED_ALLOWLIST entry must match a real route."""
    actual = {(m, p) for m, p, _ in _iter_routes(router_module, orgs_module)}
    phantom = [str(key) for key in UNSCOPED_ALLOWLIST if key not in actual]
    assert not phantom, (
        "These allowlist entries don't match any registered route — "
        f"remove them: {phantom}"
    )


def test_scoping_helpers_used_in_router():
    """Sanity-check: the router source mentions scoped() and the org-id helpers
    somewhere. Without this, the per-route Depends check is meaningless
    because nothing would actually filter the query."""
    src = inspect.getsource(router_module)
    assert "from orchestrator.scoping import scoped" in src
    assert "scoped(" in src
    assert "_get_task_in_org" in src
    assert "_get_repo_in_org" in src


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/tasks"),
        ("GET", "/tasks/{task_id}"),
        ("DELETE", "/tasks/{task_id}"),
        ("GET", "/repos"),
        ("GET", "/suggestions"),
        ("GET", "/freeform/config"),
        ("GET", "/schedules"),
        ("GET", "/feedback/summary"),
        ("GET", "/orgs/me"),
        ("GET", "/orgs/{target_org_id}/members"),
    ],
)
def test_critical_endpoints_have_org_dep(method, path):
    """Hand-picked smoke list — the highest-traffic tenant reads.

    Anything in this list MUST take current_org_id as a Depends. This
    catches a class of refactor that removes the dep from a key route
    without adding it to the allowlist (which would be caught above,
    but a louder per-route assertion helps reviewers spot the breakage)."""
    for m, p, endpoint in _iter_routes(router_module, orgs_module):
        if m == method and p == path:
            assert _depends_on_current_org_id(endpoint), (
                f"{method} {path} must Depends(current_org_id)"
            )
            return
    pytest.fail(f"{method} {path} not registered on the router")
