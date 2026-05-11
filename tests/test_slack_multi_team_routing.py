# tests/test_slack_multi_team_routing.py
"""Multi-team Slack: AsyncApp built with installation_store, no static
`token=`. Token resolution at event time goes through async_find_bot."""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub aiohttp (not installed in the test venv) and everything that chains
# off it before any import of integrations.slack.main.  The real package
# tries to import aiohttp.FormData, BasicAuth, etc. at module-load time, so
# we make the stub act like an infinitely-attribute module via __getattr__.
# ---------------------------------------------------------------------------


class _AiohttpStub(ModuleType):
    """A stub module that returns MagicMock for every attribute access."""

    def __getattr__(self, name: str):
        obj = MagicMock()
        setattr(self, name, obj)
        return obj


_aiohttp_stub = _AiohttpStub("aiohttp")
sys.modules["aiohttp"] = _aiohttp_stub

# Pre-populate a few names that are imported with `from aiohttp import ...`
# syntax (those bypass __getattr__ in some Python versions).
for _name in ("FormData", "BasicAuth", "ClientSession", "web", "TCPConnector"):
    setattr(_aiohttp_stub, _name, MagicMock())

# Also stub the socket-mode adapter that directly imports from aiohttp.
for _mod_name in (
    "slack_sdk.socket_mode.aiohttp",
    "slack_bolt.adapter.socket_mode.aiohttp",
    "slack_bolt.adapter.socket_mode.async_handler",
    "slack_bolt.adapter.aiohttp",
):
    _stub = ModuleType(_mod_name)
    _stub.AsyncSocketModeHandler = MagicMock()  # type: ignore[attr-defined]
    _stub.SocketModeClient = MagicMock()  # type: ignore[attr-defined]
    _stub.to_bolt_request = MagicMock()  # type: ignore[attr-defined]
    _stub.to_aiohttp_response = MagicMock()  # type: ignore[attr-defined]
    sys.modules[_mod_name] = _stub

# Now it is safe to import the module under test.
from integrations.slack import main as slack_main  # noqa: E402
from integrations.slack.installation_store import PostgresInstallationStore  # noqa: E402


def test_get_app_uses_installation_store_when_no_legacy_token(monkeypatch):
    # No legacy token configured → multi-team mode.
    monkeypatch.setattr(slack_main.settings, "slack_bot_token", "")
    slack_main._app = None  # reset singleton

    app = slack_main._get_app()
    assert app.installation_store is not None
    assert isinstance(app.installation_store, PostgresInstallationStore)


def test_get_app_uses_legacy_token_when_set_and_no_installations(monkeypatch):
    """Single-tenant deploys without distributed-app credentials still
    work via the legacy SLACK_BOT_TOKEN env path."""
    monkeypatch.setattr(slack_main.settings, "slack_bot_token", "xoxb-legacy")
    slack_main._app = None

    app = slack_main._get_app()
    # The legacy path uses token=, not installation_store.
    # AsyncApp stores the token in _token (slack-bolt private attribute,
    # confirmed from slack_bolt/app/async_app.py line 222 in the installed
    # version).  There is no public `.token` property on AsyncApp.
    assert getattr(app, "_token", None) == "xoxb-legacy" or getattr(app, "token", None) == "xoxb-legacy"
