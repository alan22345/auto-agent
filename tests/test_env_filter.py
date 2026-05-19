"""Tests for shared.env_filter and shared.config.reserved_env_keys — ADR-019 T3."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# reserved_env_keys()
# ---------------------------------------------------------------------------


def test_reserved_env_keys_returns_frozenset():
    from shared.config import reserved_env_keys

    result = reserved_env_keys()
    assert isinstance(result, frozenset)


def test_reserved_env_keys_are_uppercase():
    from shared.config import reserved_env_keys

    for key in reserved_env_keys():
        assert key == key.upper(), f"expected uppercase, got: {key!r}"


def test_reserved_env_keys_contains_known_settings_fields():
    from shared.config import reserved_env_keys

    keys = reserved_env_keys()
    # These fields are defined in Settings — they must appear in the reserved set.
    assert "SECRETS_PASSPHRASE" in keys
    assert "ANTHROPIC_API_KEY" in keys
    assert "DATABASE_URL" in keys
    assert "JWT_SECRET" in keys
    assert "GITHUB_TOKEN" in keys


def test_reserved_env_keys_is_nonempty():
    from shared.config import reserved_env_keys

    assert len(reserved_env_keys()) > 10


# ---------------------------------------------------------------------------
# filtered_host_env()
# ---------------------------------------------------------------------------


def test_filtered_host_env_removes_reserved_key(monkeypatch):
    """A key declared in Settings must be stripped from the result."""
    from shared.config import reserved_env_keys
    from shared.env_filter import filtered_host_env

    # Pick any key we know is reserved.
    reserved = next(iter(reserved_env_keys()))
    monkeypatch.setenv(reserved, "should-be-gone")

    result = filtered_host_env()
    assert reserved not in result, f"{reserved!r} should have been filtered out"


def test_filtered_host_env_preserves_non_reserved_key(monkeypatch):
    """A key that is NOT in reserved_env_keys must pass through."""
    from shared.env_filter import filtered_host_env

    monkeypatch.setenv("HOME", "/tmp/test_home")

    result = filtered_host_env()
    # HOME is not a Settings field — it must survive.
    assert "HOME" in result
    assert result["HOME"] == "/tmp/test_home"


def test_filtered_host_env_case_insensitive_filtering(monkeypatch):
    """Lower-case env var that maps to a reserved key is also filtered."""
    from shared.env_filter import filtered_host_env

    # SECRETS_PASSPHRASE is in reserved_env_keys() as uppercase.
    # If someone sets it lowercase, it should still be filtered.
    monkeypatch.setenv("secrets_passphrase", "lower-case-value")

    result = filtered_host_env()
    assert "secrets_passphrase" not in result, (
        "lowercase 'secrets_passphrase' should be filtered (case-insensitive check)"
    )


def test_filtered_host_env_case_insensitive_filtering_anthropic(monkeypatch):
    """ANTHROPIC_API_KEY set lowercase is filtered."""
    from shared.env_filter import filtered_host_env

    monkeypatch.setenv("anthropic_api_key", "sk-ant-lower")

    result = filtered_host_env()
    assert "anthropic_api_key" not in result


def test_filtered_host_env_returns_dict_of_strings(monkeypatch):
    """Return type is dict[str, str]."""
    from shared.env_filter import filtered_host_env

    result = filtered_host_env()
    assert isinstance(result, dict)
    for k, v in result.items():
        assert isinstance(k, str)
        assert isinstance(v, str)


def test_filtered_host_env_does_not_mutate_os_environ(monkeypatch):
    """The function must return a copy, not mutate os.environ."""
    import os

    from shared.env_filter import filtered_host_env

    before = dict(os.environ)
    filtered_host_env()
    after = dict(os.environ)
    assert before == after
