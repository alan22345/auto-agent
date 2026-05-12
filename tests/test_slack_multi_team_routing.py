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
    assert (
        getattr(app, "_token", None) == "xoxb-legacy"
        or getattr(app, "token", None) == "xoxb-legacy"
    )


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_send_slack_dm_uses_per_org_bot_token(monkeypatch):
    """In multi-team mode, send_slack_dm(slack_user_id, text, org_id=42)
    fetches org 42's bot_token from the installation store, then posts
    via an AsyncWebClient initialised with that token — NOT via the
    singleton app.client."""
    from slack_sdk.web.async_client import AsyncWebClient

    monkeypatch.setattr(slack_main.settings, "slack_bot_token", "")
    slack_main._app = None

    async def fake_bot_token(org_id):
        return "xoxb-org42"

    open_resp = {"channel": {"id": "D1"}}
    post_resp = {"ts": "1.0"}

    posts = []

    async def fake_open(users):
        return open_resp

    async def fake_post(channel, text, mrkdwn):
        posts.append({"channel": channel, "text": text})
        return post_resp

    original_init = AsyncWebClient.__init__

    init_calls = []

    def capture_init(self, token=None, **kw):
        init_calls.append(token)
        original_init(self, token=token, **kw)
        self.conversations_open = fake_open
        self.chat_postMessage = fake_post

    monkeypatch.setattr(AsyncWebClient, "__init__", capture_init)
    monkeypatch.setattr(slack_main, "_bot_token_for_org", fake_bot_token, raising=False)

    await slack_main.send_slack_dm("UTARGET", "hello", org_id=42)

    assert init_calls == ["xoxb-org42"]
    assert posts and posts[0]["channel"] == "D1"


@pytest.mark.asyncio
async def test_handle_dm_resolves_org_id_from_team(monkeypatch):
    """DM events arrive tagged with `team`/`team_id`. The handler must
    resolve that to an org_id BEFORE looking up the linked user."""
    monkeypatch.setattr(slack_main.settings, "slack_bot_token", "")

    captured_org = {}

    async def fake_org_for_team(team_id):
        return 42 if team_id == "T42" else None

    async def fake_user_for_slack(slack_user_id, *, org_id=None):
        captured_org["user_org"] = org_id
        return {"id": 1, "username": "alice", "display_name": "Alice"}

    async def fake_send(slack_user_id, msg, *, task_id=None, org_id=None):
        return None

    async def fake_converse(slack_user_id, user_id, text, *, org_id=None):
        captured_org["converse_org"] = org_id
        return ""

    monkeypatch.setattr(slack_main, "_org_for_team", fake_org_for_team, raising=False)
    monkeypatch.setattr(slack_main, "_user_for_slack_id", fake_user_for_slack)
    monkeypatch.setattr(slack_main, "send_slack_dm", fake_send)

    import sys
    import types

    mod = types.ModuleType("agent.slack_assistant")
    mod.converse = fake_converse
    sys.modules["agent.slack_assistant"] = mod

    await slack_main._handle_dm_event(
        {
            "team": "T42",
            "channel_type": "im",
            "user": "U1",
            "text": "hello",
        }
    )
    assert captured_org["user_org"] == 42
    assert captured_org["converse_org"] == 42


@pytest.mark.asyncio
async def test_handle_dm_drops_unknown_team(monkeypatch):
    """Events from a workspace we don't have an installation for are dropped."""
    monkeypatch.setattr(slack_main.settings, "slack_bot_token", "")

    async def fake_org_for_team(team_id):
        return None

    monkeypatch.setattr(slack_main, "_org_for_team", fake_org_for_team, raising=False)

    result = await slack_main._handle_dm_event(
        {"team": "T_UNKNOWN", "channel_type": "im", "user": "U1", "text": "hello"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_user_for_slack_id_filters_by_org_membership(monkeypatch):
    """A user must be a member of the org their event came from. If not,
    return None (treat as unlinked) — never silently bind across orgs."""

    # Simulate: User row exists with slack_user_id=U_ALICE; user is NOT
    # a member of org 42. The DB returns no row from the JOIN.
    captured_sql = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=None):
            captured_sql.append(str(sql))
            m = MagicMock()
            m.first = lambda: None
            m.scalar_one_or_none = lambda: None
            return m

    monkeypatch.setattr(slack_main, "async_session", lambda: FakeSession())

    user = await slack_main._user_for_slack_id("U_ALICE", org_id=42)
    assert user is None
    joined = " ".join(captured_sql)
    # The query must join through organization_memberships when org_id is set.
    assert "organization_memberships" in joined.lower() or "JOIN" in joined.upper()


def test_converse_signature_v2():
    """converse takes history + on_create_task; org_id is removed (router-owned)."""
    import inspect
    import sys

    # Clean up any mocked module from prior tests to get the real one.
    if "agent.slack_assistant" in sys.modules:
        del sys.modules["agent.slack_assistant"]
    from agent.slack_assistant import converse

    sig = inspect.signature(converse)
    params = sig.parameters
    # New required kwargs.
    assert "user_id" in params
    assert "text" in params
    assert "history" in params
    assert "home_dir" in params
    # Optional callback.
    assert "on_create_task" in params
    # Removed.
    assert "org_id" not in params
    assert "slack_user_id" not in params
